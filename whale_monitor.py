#!/usr/bin/env python3
"""
Polymarket Whale Monitor
========================

This script monitors Polymarket for "Fresh Whale" activity - detecting when
newly created accounts suddenly invest large sums (>$10,000 USD) into markets.

Data Sources:
- Activity Subgraph: Large trade detection (splits)
- Gamma API: Market information lookup
- Data API: User trading history

Alerting: Discord Webhook notifications

Author: Polymarket Outlier Tracker
License: MIT
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any, Set
from dataclasses import dataclass, field

import requests

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class Config:
    """Configuration settings for the Whale Monitor."""

    # API Endpoints
    ACTIVITY_SUBGRAPH_URL: str = (
        "https://api.goldsky.com/api/public/"
        "project_cl6mb8i9h0003e201j6li0diw/subgraphs/activity-subgraph/0.0.4/gn"
    )
    GAMMA_API_URL: str = "https://gamma-api.polymarket.com"
    DATA_API_URL: str = "https://data-api.polymarket.com"

    # Discord webhook URL (set via environment variable or directly)
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # Thresholds for "Fresh Whale" detection
    MIN_TRADE_VALUE_USD: float = 10_000.0  # Minimum trade value to trigger alert
    MAX_HISTORICAL_TRADES: int = 5  # Max trades for "new account"
    NEW_ACCOUNT_HOURS: int = 72  # Account age threshold in hours

    # Polling configuration
    POLL_INTERVAL_SECONDS: int = 60  # How often to check for new trades
    LOOKBACK_MINUTES: int = 5  # How far back to look for trades each poll

    # API settings
    REQUEST_TIMEOUT: int = 30  # HTTP request timeout in seconds
    MAX_RETRIES: int = 3  # Max retries for failed requests
    RETRY_DELAY: int = 5  # Delay between retries in seconds
    RATE_LIMIT_DELAY: float = 0.2  # Delay between API calls

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Aggregate Asymmetric Whale (AAW) Detection
    AGGREGATE_MIN_POSITION_USD: float = 30_000.0  # Min aggregate position value
    ASYMMETRIC_PRICE_THRESHOLD: float = 0.30  # Max avg price for asymmetric (low-odds)
    AGGREGATE_LOOKBACK_DAYS: int = 14  # Days to look back for position building
    AGGREGATE_SCAN_INTERVAL_MINUTES: int = 30  # How often to run aggregate scan
    MAX_SINGLE_TRADE_FOR_AGGREGATE: float = 10_000.0  # Max single trade (stealth)
    ENABLE_AGGREGATE_DETECTION: bool = True  # Enable/disable aggregate detection

    def validate(self) -> bool:
        """Validate critical configuration settings."""
        if not self.DISCORD_WEBHOOK_URL:
            logging.warning(
                "DISCORD_WEBHOOK_URL not set. Alerts will only be logged locally."
            )
        return True


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Trade:
    """Represents a trading event on Polymarket."""
    id: str
    user_address: str
    market_id: str
    market_title: str
    outcome: str  # "Yes" or "No"
    amount: float  # Number of shares
    price: float  # Price per share (0-1)
    value_usd: float  # Total value in USD
    timestamp: int  # Unix timestamp
    tx_hash: str
    username: str = ""  # Polymarket username if available

    @property
    def formatted_time(self) -> str:
        """Return human-readable timestamp."""
        return datetime.fromtimestamp(
            self.timestamp, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class AccountProfile:
    """Profile information for a Polymarket account."""
    address: str
    total_trades: int
    first_trade_timestamp: Optional[int]
    total_volume_usd: float
    markets_traded: int
    username: str = ""  # Polymarket username if available

    @property
    def account_age_hours(self) -> Optional[float]:
        """Calculate account age in hours from first trade."""
        if not self.first_trade_timestamp:
            return None
        age_seconds = time.time() - self.first_trade_timestamp
        return age_seconds / 3600

    def is_fresh_whale(self, config: Config) -> bool:
        """Determine if this account qualifies as a 'Fresh Whale'."""
        # Check if account has few historical trades
        if self.total_trades < config.MAX_HISTORICAL_TRADES:
            return True

        # Check if account is new (first activity within threshold)
        if self.account_age_hours is not None:
            if self.account_age_hours < config.NEW_ACCOUNT_HOURS:
                return True

        return False


@dataclass
class FreshWhaleAlert:
    """Alert data for a Fresh Whale detection."""
    trade: Trade
    profile: AccountProfile
    detection_reason: str
    alert_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_discord_embed(self) -> Dict[str, Any]:
        """Format alert as Discord embed."""
        # Determine embed color based on trade size
        if self.trade.value_usd >= 100_000:
            color = 0xFF0000  # Red for mega whales
        elif self.trade.value_usd >= 50_000:
            color = 0xFFA500  # Orange for large whales
        else:
            color = 0x00FF00  # Green for threshold whales

        # Build Polymarket profile URL
        profile_url = f"https://polymarket.com/profile/{self.trade.user_address}"

        # Format account age
        if self.profile.account_age_hours is not None:
            if self.profile.account_age_hours < 1:
                age_str = f"{int(self.profile.account_age_hours * 60)} minutes"
            elif self.profile.account_age_hours < 24:
                age_str = f"{self.profile.account_age_hours:.1f} hours"
            else:
                age_str = f"{self.profile.account_age_hours / 24:.1f} days"
        else:
            age_str = "Unknown"

        embed = {
            "title": "Fresh Whale Detected!",
            "description": (
                f"A new account just made a large trade on Polymarket."
            ),
            "color": color,
            "fields": [
                {
                    "name": "Market",
                    "value": self.trade.market_title[:256],  # Discord limit
                    "inline": False
                },
                {
                    "name": "Position",
                    "value": f"**{self.trade.outcome}** @ ${self.trade.price:.3f}",
                    "inline": True
                },
                {
                    "name": "Amount Invested",
                    "value": f"**${self.trade.value_usd:,.2f}**",
                    "inline": True
                },
                {
                    "name": "Shares Purchased",
                    "value": f"{self.trade.amount:,.2f}",
                    "inline": True
                },
                {
                    "name": "Account Age",
                    "value": age_str,
                    "inline": True
                },
                {
                    "name": "Total Prior Trades",
                    "value": str(self.profile.total_trades),
                    "inline": True
                },
                {
                    "name": "Detection Reason",
                    "value": self.detection_reason,
                    "inline": True
                },
                {
                    "name": "Wallet Address",
                    "value": f"`{self.trade.user_address[:10]}...{self.trade.user_address[-8:]}`",
                    "inline": False
                }
            ],
            "timestamp": self.alert_time.isoformat(),
            "footer": {
                "text": "Polymarket Whale Monitor"
            },
            "url": profile_url
        }

        # Add username field if available
        if self.trade.username or self.profile.username:
            username = self.trade.username or self.profile.username
            embed["fields"].insert(0, {
                "name": "Username",
                "value": f"**{username}**",
                "inline": True
            })

        return embed


@dataclass
class MarketPosition:
    """Aggregated position in a single market."""
    market_id: str
    market_title: str
    outcome: str  # "Yes" or "No"
    total_shares: float
    average_price: float  # Weighted average entry price
    total_invested_usd: float
    trade_count: int
    first_trade_timestamp: int
    last_trade_timestamp: int
    max_single_trade_usd: float

    @property
    def formatted_first_trade(self) -> str:
        return datetime.fromtimestamp(
            self.first_trade_timestamp, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

    @property
    def formatted_last_trade(self) -> str:
        return datetime.fromtimestamp(
            self.last_trade_timestamp, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

    @property
    def potential_payout(self) -> float:
        """Potential payout if position wins (shares * $1)."""
        return self.total_shares

    @property
    def implied_edge(self) -> float:
        """Implied edge if position wins: (payout - invested) / invested."""
        if self.total_invested_usd == 0:
            return 0
        return (self.potential_payout - self.total_invested_usd) / self.total_invested_usd


@dataclass
class AggregateWhaleAlert:
    """Alert data for Aggregate Asymmetric Whale detection."""
    user_address: str
    position: MarketPosition
    profile: AccountProfile
    detection_reason: str
    alert_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_discord_embed(self) -> Dict[str, Any]:
        """Format alert as Discord embed with purple color for aggregate whales."""
        color = 0x9B59B6  # Purple for aggregate whales

        profile_url = f"https://polymarket.com/profile/{self.user_address}"

        # Format account age
        if self.profile.account_age_hours is not None:
            if self.profile.account_age_hours < 1:
                age_str = f"{int(self.profile.account_age_hours * 60)} minutes"
            elif self.profile.account_age_hours < 24:
                age_str = f"{self.profile.account_age_hours:.1f} hours"
            else:
                age_str = f"{self.profile.account_age_hours / 24:.1f} days"
        else:
            age_str = "Unknown"

        # Calculate position building period
        build_seconds = self.position.last_trade_timestamp - self.position.first_trade_timestamp
        if build_seconds < 3600:
            build_period = f"{int(build_seconds / 60)} minutes"
        elif build_seconds < 86400:
            build_period = f"{build_seconds / 3600:.1f} hours"
        else:
            build_period = f"{build_seconds / 86400:.1f} days"

        embed = {
            "title": "Aggregate Whale Detected!",
            "description": (
                "Account built large position through multiple small trades in low-odds market."
            ),
            "color": color,
            "fields": [
                {
                    "name": "Market",
                    "value": self.position.market_title[:256],
                    "inline": False
                },
                {
                    "name": "Position",
                    "value": f"**{self.position.outcome}** @ avg ${self.position.average_price:.3f}",
                    "inline": True
                },
                {
                    "name": "Total Invested",
                    "value": f"**${self.position.total_invested_usd:,.2f}**",
                    "inline": True
                },
                {
                    "name": "Potential Payout",
                    "value": f"${self.position.potential_payout:,.2f}",
                    "inline": True
                },
                {
                    "name": "Trade Count",
                    "value": str(self.position.trade_count),
                    "inline": True
                },
                {
                    "name": "Max Single Trade",
                    "value": f"${self.position.max_single_trade_usd:,.2f}",
                    "inline": True
                },
                {
                    "name": "Build Period",
                    "value": build_period,
                    "inline": True
                },
                {
                    "name": "Account Age",
                    "value": age_str,
                    "inline": True
                },
                {
                    "name": "Total Prior Trades",
                    "value": str(self.profile.total_trades),
                    "inline": True
                },
                {
                    "name": "Implied Edge",
                    "value": f"{self.position.implied_edge * 100:.1f}%",
                    "inline": True
                },
                {
                    "name": "Detection Reason",
                    "value": self.detection_reason,
                    "inline": False
                },
                {
                    "name": "Wallet Address",
                    "value": f"`{self.user_address[:10]}...{self.user_address[-8:]}`",
                    "inline": False
                }
            ],
            "timestamp": self.alert_time.isoformat(),
            "footer": {
                "text": "Polymarket Whale Monitor - Aggregate Detection"
            },
            "url": profile_url
        }

        # Add username field if available
        if self.profile.username:
            embed["fields"].insert(0, {
                "name": "Username",
                "value": f"**{self.profile.username}**",
                "inline": True
            })

        return embed


# =============================================================================
# POLYMARKET API CLIENT
# =============================================================================

class PolymarketClient:
    """
    Client for interacting with Polymarket APIs.

    Uses multiple endpoints:
    - Activity Subgraph: For detecting large trades (splits)
    - Data API: For user trading history and market information
    """

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)

        # Cache for market info to reduce API calls (condition_id -> {title, ...})
        self._market_cache: Dict[str, Dict] = {}
        # Cache for user activity
        self._user_activity_cache: Dict[str, List[Dict]] = {}

    def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> Optional[requests.Response]:
        """Make HTTP request with retry logic."""
        kwargs.setdefault("timeout", self.config.REQUEST_TIMEOUT)

        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                self.logger.warning(
                    f"Request failed (attempt {attempt + 1}/{self.config.MAX_RETRIES}): {e}"
                )
                if attempt < self.config.MAX_RETRIES - 1:
                    time.sleep(self.config.RETRY_DELAY)
                else:
                    self.logger.error(f"All retry attempts failed: {e}")
                    return None
        return None

    def _graphql_query(self, query: str, variables: Optional[Dict] = None) -> Optional[Dict]:
        """Execute GraphQL query against Activity Subgraph."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        response = self._request_with_retry(
            "POST",
            self.config.ACTIVITY_SUBGRAPH_URL,
            json=payload,
            headers={"Content-Type": "application/json"}
        )

        if not response:
            return None

        result = response.json()
        if "errors" in result:
            self.logger.error(f"GraphQL errors: {result['errors']}")
            return None

        return result.get("data")

    def get_large_splits(
        self,
        since_timestamp: int,
        min_amount_raw: int,
        first: int = 100
    ) -> List[Dict]:
        """
        Fetch large splits (trades) from Activity Subgraph.

        Args:
            since_timestamp: Unix timestamp to look back from
            min_amount_raw: Minimum amount in raw units (6 decimals for USDC)
            first: Max number of results

        Returns:
            List of split records
        """
        query = """
        query GetLargeSplits($since: BigInt!, $minAmount: BigInt!, $first: Int!) {
            splits(
                first: $first,
                orderBy: timestamp,
                orderDirection: desc,
                where: {
                    timestamp_gte: $since,
                    amount_gte: $minAmount
                }
            ) {
                id
                timestamp
                stakeholder
                condition
                amount
            }
        }
        """

        variables = {
            "since": str(since_timestamp),
            "minAmount": str(min_amount_raw),
            "first": first
        }

        data = self._graphql_query(query, variables)

        if data and "splits" in data:
            return data["splits"]

        return []

    def get_market_info(self, condition_id: str) -> Optional[Dict]:
        """
        Get market information from Gamma API by condition ID.

        Args:
            condition_id: The condition ID from the subgraph

        Returns:
            Market info dict or None
        """
        # Check cache first
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        time.sleep(self.config.RATE_LIMIT_DELAY)

        url = f"{self.config.GAMMA_API_URL}/markets"
        params = {"conditionId": condition_id}

        response = self._request_with_retry("GET", url, params=params)

        if response and response.status_code == 200:
            markets = response.json()
            if markets and len(markets) > 0:
                market = markets[0]
                self._market_cache[condition_id] = market
                return market

        return None

    def get_user_activity(self, user_address: str, limit: int = 100) -> List[Dict]:
        """
        Get user's trading activity from Data API.

        Also caches market info (title) from activity for later use.

        Args:
            user_address: User's wallet address
            limit: Max number of activities to fetch

        Returns:
            List of activity records
        """
        address = user_address.lower()

        # Check cache first
        if address in self._user_activity_cache:
            return self._user_activity_cache[address]

        time.sleep(self.config.RATE_LIMIT_DELAY)

        url = f"{self.config.DATA_API_URL}/activity"
        params = {"user": address, "limit": limit}

        response = self._request_with_retry("GET", url, params=params)

        if response and response.status_code == 200:
            activities = response.json()

            # Cache market info from activities
            for act in activities:
                cond_id = act.get("conditionId")
                title = act.get("title")
                if cond_id and title and cond_id not in self._market_cache:
                    self._market_cache[cond_id] = {
                        "question": title,
                        "slug": act.get("slug", ""),
                        "outcome": act.get("outcome", ""),
                    }

            self._user_activity_cache[address] = activities
            return activities

        return []

    def get_username_from_activity(self, activities: List[Dict]) -> str:
        """Extract username from activity data."""
        if activities:
            # The 'name' field contains the username
            return activities[0].get("name", "") or ""
        return ""

    def get_account_profile(self, address: str) -> Optional[AccountProfile]:
        """
        Build account profile from user's trading history.

        Args:
            address: User's wallet address

        Returns:
            AccountProfile object or None
        """
        activities = self.get_user_activity(address, limit=1000)

        # Extract username from activity data
        username = self.get_username_from_activity(activities)

        if not activities:
            # No history found - truly new account
            return AccountProfile(
                address=address,
                total_trades=0,
                first_trade_timestamp=None,
                total_volume_usd=0,
                markets_traded=0,
                username=username
            )

        # Filter for TRADE type activities
        trades = [a for a in activities if a.get("type") == "TRADE"]

        if not trades:
            return AccountProfile(
                address=address,
                total_trades=0,
                first_trade_timestamp=None,
                total_volume_usd=0,
                markets_traded=0,
                username=username
            )

        # Sort by timestamp ascending to find first trade
        trades_sorted = sorted(trades, key=lambda x: x.get("timestamp", 0))

        total_trades = len(trades)
        first_trade_ts = trades_sorted[0].get("timestamp") if trades_sorted else None
        total_volume = sum(t.get("usdcSize", 0) for t in trades)
        unique_markets = set(t.get("conditionId", "") for t in trades)

        return AccountProfile(
            address=address,
            total_trades=total_trades,
            first_trade_timestamp=first_trade_ts,
            total_volume_usd=total_volume,
            markets_traded=len(unique_markets),
            username=username
        )

    def get_recent_trades(
        self,
        since_timestamp: int,
        min_value_usd: float = 0,
        first: int = 100
    ) -> List[Trade]:
        """
        Fetch recent large trades.

        Combines data from:
        - Activity Subgraph (splits) for trade detection
        - Data API for market information and usernames

        Args:
            since_timestamp: Look for trades after this timestamp
            min_value_usd: Minimum trade value in USD
            first: Max results

        Returns:
            List of Trade objects
        """
        # Convert USD to raw amount (6 decimals for USDC)
        min_amount_raw = int(min_value_usd * 1_000_000)

        # Get large splits from subgraph
        splits = self.get_large_splits(since_timestamp, min_amount_raw, first)

        self.logger.info(f"Found {len(splits)} large splits from subgraph")

        trades = []
        for split in splits:
            try:
                # Parse split data
                split_id = split["id"]
                timestamp = int(split["timestamp"])
                stakeholder = split["stakeholder"]
                condition = split["condition"]
                amount_raw = int(split["amount"])
                value_usd = amount_raw / 1_000_000  # Convert from raw to USD

                # Get user activity to populate market cache and get username
                activities = self.get_user_activity(stakeholder, limit=100)
                username = self.get_username_from_activity(activities)

                # Try to get market info from cache (populated by user activity)
                market_info = self._market_cache.get(condition)

                if market_info:
                    market_title = market_info.get("question", "Unknown Market")
                    market_id = condition
                else:
                    # Fallback: try Gamma API (may not be reliable)
                    gamma_info = self.get_market_info(condition)
                    if gamma_info:
                        market_title = gamma_info.get("question", "Unknown Market")
                        market_id = gamma_info.get("id", condition)
                    else:
                        market_title = f"Market {condition[:16]}..."
                        market_id = condition

                # Create Trade object
                trade = Trade(
                    id=split_id,
                    user_address=stakeholder,
                    market_id=market_id,
                    market_title=market_title,
                    outcome=market_info.get("outcome", "Position") if market_info else "Position",
                    amount=amount_raw / 1_000_000,  # Shares
                    price=1.0,  # Unknown from splits
                    value_usd=value_usd,
                    timestamp=timestamp,
                    tx_hash=split_id.split("_")[0] if "_" in split_id else split_id,
                    username=username
                )
                trades.append(trade)

            except (KeyError, ValueError, TypeError) as e:
                self.logger.warning(f"Failed to parse split: {e}")
                continue

        return trades


# =============================================================================
# DISCORD ALERTING
# =============================================================================

class DiscordNotifier:
    """Sends alerts to Discord via webhook."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()

    def send_alert(self, alert) -> bool:
        """Send a Fresh Whale or Aggregate Whale alert to Discord."""
        if not self.config.DISCORD_WEBHOOK_URL:
            self.logger.warning("Discord webhook not configured, skipping notification")
            return False

        embed = alert.to_discord_embed()

        payload = {
            "username": "Polymarket Whale Monitor",
            "embeds": [embed]
        }

        try:
            response = self.session.post(
                self.config.DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=self.config.REQUEST_TIMEOUT
            )

            if response.status_code == 204:
                self.logger.info(
                    f"Alert sent for ${alert.trade.value_usd:,.2f} trade by "
                    f"{alert.trade.user_address[:10]}..."
                )
                return True
            else:
                self.logger.error(
                    f"Discord webhook failed: {response.status_code} - {response.text}"
                )
                return False

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to send Discord alert: {e}")
            return False

    def send_startup_notification(self) -> bool:
        """Send a notification that the monitor has started."""
        if not self.config.DISCORD_WEBHOOK_URL:
            return False

        payload = {
            "username": "Polymarket Whale Monitor",
            "embeds": [{
                "title": "Whale Monitor Started",
                "description": (
                    f"Monitoring for trades above **${self.config.MIN_TRADE_VALUE_USD:,.0f}**\n"
                    f"Polling every **{self.config.POLL_INTERVAL_SECONDS}** seconds"
                ),
                "color": 0x0099FF,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Polymarket Whale Monitor"}
            }]
        }

        try:
            response = self.session.post(
                self.config.DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=self.config.REQUEST_TIMEOUT
            )
            return response.status_code == 204
        except requests.exceptions.RequestException:
            return False


# =============================================================================
# MAIN MONITOR
# =============================================================================

class WhaleMonitor:
    """Main monitoring orchestrator."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.config.validate()

        self.client = PolymarketClient(self.config)
        self.notifier = DiscordNotifier(self.config)
        self.logger = logging.getLogger(self.__class__.__name__)

        # Track processed trades to avoid duplicate alerts
        self.processed_trade_ids: Set[str] = set()

        # Track alerted aggregate positions to avoid duplicates
        # Key: (address, market_id, outcome)
        self.alerted_aggregate_positions: Set[tuple] = set()

        # Track last aggregate scan time
        self.last_aggregate_scan: Optional[datetime] = None

        # Statistics
        self.stats = {
            "total_trades_scanned": 0,
            "large_trades_found": 0,
            "fresh_whales_detected": 0,
            "aggregate_whales_detected": 0,
            "alerts_sent": 0,
            "start_time": None
        }

    def _setup_logging(self):
        """Configure logging for the monitor."""
        log_level = getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO)

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

    def process_trade(self, trade: Trade) -> Optional[FreshWhaleAlert]:
        """
        Process a single trade and determine if it's a Fresh Whale.

        Returns an alert if the trade qualifies, None otherwise.
        """
        # Skip if already processed
        if trade.id in self.processed_trade_ids:
            return None

        self.processed_trade_ids.add(trade.id)
        self.stats["total_trades_scanned"] += 1

        # Check if trade meets value threshold
        if trade.value_usd < self.config.MIN_TRADE_VALUE_USD:
            return None

        self.stats["large_trades_found"] += 1
        self.logger.info(
            f"Large trade detected: ${trade.value_usd:,.2f} on '{trade.market_title[:50]}...'"
        )

        # Fetch account profile to check if "fresh"
        profile = self.client.get_account_profile(trade.user_address)

        if profile is None:
            self.logger.warning(f"Could not fetch profile for {trade.user_address}")
            return None

        # Check if account qualifies as a Fresh Whale
        if not profile.is_fresh_whale(self.config):
            self.logger.info(
                f"Account {trade.user_address[:10]}... has "
                f"{profile.total_trades} trades, not a fresh whale"
            )
            return None

        # Determine the reason for flagging
        if profile.total_trades < self.config.MAX_HISTORICAL_TRADES:
            reason = f"Only {profile.total_trades} prior trades"
        elif profile.account_age_hours is not None and \
             profile.account_age_hours < self.config.NEW_ACCOUNT_HOURS:
            reason = f"Account only {profile.account_age_hours:.1f} hours old"
        else:
            reason = "New account pattern detected"

        self.stats["fresh_whales_detected"] += 1

        alert = FreshWhaleAlert(
            trade=trade,
            profile=profile,
            detection_reason=reason
        )

        self.logger.warning(
            f"FRESH WHALE DETECTED: ${trade.value_usd:,.2f} by "
            f"{trade.user_address[:10]}... - {reason}"
        )

        return alert

    def run_single_poll(self) -> List[FreshWhaleAlert]:
        """Run a single polling cycle and return any alerts generated."""
        alerts = []

        # Calculate timestamp for lookback period
        lookback_seconds = self.config.LOOKBACK_MINUTES * 60
        since_timestamp = int(time.time()) - lookback_seconds

        # Fetch recent trades
        trades = self.client.get_recent_trades(
            since_timestamp=since_timestamp,
            min_value_usd=self.config.MIN_TRADE_VALUE_USD
        )

        # Process each trade
        for trade in trades:
            alert = self.process_trade(trade)
            if alert:
                alerts.append(alert)

                # Send notification
                if self.notifier.send_alert(alert):
                    self.stats["alerts_sent"] += 1

        return alerts

    # =========================================================================
    # AGGREGATE ASYMMETRIC WHALE (AAW) DETECTION
    # =========================================================================

    def get_account_positions(self, address: str) -> List[MarketPosition]:
        """
        Fetch all trades for an account and aggregate by market/outcome.

        Returns a list of MarketPosition objects representing the account's
        aggregated positions in each market.
        """
        activities = self.client.get_user_activity(address, limit=1000)

        if not activities:
            return []

        # Filter for TRADE type activities within lookback period
        lookback_ts = int(time.time()) - (self.config.AGGREGATE_LOOKBACK_DAYS * 86400)
        trades = [
            a for a in activities
            if a.get("type") == "TRADE" and a.get("timestamp", 0) >= lookback_ts
        ]

        if not trades:
            return []

        # Aggregate trades by (market_id, outcome)
        # Key: (conditionId, outcome) -> list of trades
        position_map: Dict[tuple, List[Dict]] = {}

        for trade in trades:
            cond_id = trade.get("conditionId", "")
            outcome = trade.get("outcome", "Unknown")
            key = (cond_id, outcome)

            if key not in position_map:
                position_map[key] = []
            position_map[key].append(trade)

        # Build MarketPosition objects
        positions = []
        for (cond_id, outcome), trade_list in position_map.items():
            # Calculate aggregates
            total_shares = 0.0
            total_invested = 0.0
            max_single_trade = 0.0
            first_ts = float('inf')
            last_ts = 0
            market_title = "Unknown Market"

            for trade in trade_list:
                shares = trade.get("outcomeTokensAmount", 0)
                # Convert from raw (6 decimals) to actual
                if isinstance(shares, str):
                    shares = float(shares)
                shares = shares / 1_000_000 if shares > 1000 else shares

                usdc_size = trade.get("usdcSize", 0)
                ts = trade.get("timestamp", 0)

                total_shares += shares
                total_invested += usdc_size
                max_single_trade = max(max_single_trade, usdc_size)
                first_ts = min(first_ts, ts)
                last_ts = max(last_ts, ts)

                # Get market title from cache or trade
                if market_title == "Unknown Market":
                    title = trade.get("title", "")
                    if title:
                        market_title = title
                    elif cond_id in self.client._market_cache:
                        market_title = self.client._market_cache[cond_id].get(
                            "question", "Unknown Market"
                        )

            # Calculate weighted average price
            if total_shares > 0:
                avg_price = total_invested / total_shares
            else:
                avg_price = 0

            if first_ts == float('inf'):
                first_ts = 0

            position = MarketPosition(
                market_id=cond_id,
                market_title=market_title,
                outcome=outcome,
                total_shares=total_shares,
                average_price=avg_price,
                total_invested_usd=total_invested,
                trade_count=len(trade_list),
                first_trade_timestamp=int(first_ts),
                last_trade_timestamp=int(last_ts),
                max_single_trade_usd=max_single_trade
            )
            positions.append(position)

        return positions

    def get_recent_active_accounts(self) -> Set[str]:
        """
        Find accounts with recent activity that might be aggregate whales.

        Uses the Activity Subgraph to find accounts with recent trades,
        looking for smaller trades that could be part of accumulation.
        """
        active_accounts: Set[str] = set()

        # Look back over the aggregate lookback period
        lookback_ts = int(time.time()) - (self.config.AGGREGATE_LOOKBACK_DAYS * 86400)

        # Query for splits (we use a lower threshold to catch accumulation)
        # Use $1000 as minimum to avoid too much noise
        min_amount_raw = int(1000 * 1_000_000)

        query = """
        query GetRecentSplits($since: BigInt!, $minAmount: BigInt!, $first: Int!) {
            splits(
                first: $first,
                orderBy: timestamp,
                orderDirection: desc,
                where: {
                    timestamp_gte: $since,
                    amount_gte: $minAmount
                }
            ) {
                stakeholder
                amount
            }
        }
        """

        variables = {
            "since": str(lookback_ts),
            "minAmount": str(min_amount_raw),
            "first": 500  # Get up to 500 recent splits
        }

        data = self.client._graphql_query(query, variables)

        if data and "splits" in data:
            # Track cumulative value per account
            account_volume: Dict[str, float] = {}

            for split in data["splits"]:
                addr = split["stakeholder"]
                amount = int(split["amount"]) / 1_000_000

                if addr not in account_volume:
                    account_volume[addr] = 0
                account_volume[addr] += amount

            # Filter to accounts with significant cumulative activity
            # but below single-trade detection threshold
            for addr, volume in account_volume.items():
                if volume >= self.config.AGGREGATE_MIN_POSITION_USD:
                    active_accounts.add(addr)

        return active_accounts

    def check_aggregate_whale(
        self,
        address: str,
        positions: List[MarketPosition],
        profile: AccountProfile
    ) -> List[AggregateWhaleAlert]:
        """
        Check if an account qualifies as an Aggregate Asymmetric Whale.

        Detection criteria (ALL must be met):
        1. Aggregate position value >= $30,000 in a single market
        2. Average entry price < 30% (low-odds/asymmetric position)
        3. No single trade exceeds $10,000 (stealth accumulation)
        4. Position built through 2+ trades
        5. Fresh account: <10 trades OR <72 hours old

        Returns list of alerts for qualifying positions.
        """
        alerts = []

        # Check fresh account criteria (using relaxed threshold for aggregate)
        # For aggregate, we use <10 trades OR <72h old
        is_fresh = (
            profile.total_trades < 10 or
            (profile.account_age_hours is not None and
             profile.account_age_hours < self.config.NEW_ACCOUNT_HOURS)
        )

        if not is_fresh:
            return []

        for position in positions:
            # Skip if already alerted
            position_key = (address, position.market_id, position.outcome)
            if position_key in self.alerted_aggregate_positions:
                continue

            # Check all criteria
            # 1. Aggregate position value >= threshold
            if position.total_invested_usd < self.config.AGGREGATE_MIN_POSITION_USD:
                continue

            # 2. Asymmetric market (low-odds position)
            if position.average_price > self.config.ASYMMETRIC_PRICE_THRESHOLD:
                continue

            # 3. Stealth accumulation (no single trade exceeds threshold)
            if position.max_single_trade_usd >= self.config.MAX_SINGLE_TRADE_FOR_AGGREGATE:
                continue

            # 4. Multiple trades
            if position.trade_count < 2:
                continue

            # All criteria met - this is an Aggregate Whale!
            self.alerted_aggregate_positions.add(position_key)

            # Build detection reason
            reasons = []
            if profile.total_trades < 10:
                reasons.append(f"only {profile.total_trades} total trades")
            if profile.account_age_hours is not None and \
               profile.account_age_hours < self.config.NEW_ACCOUNT_HOURS:
                reasons.append(f"account {profile.account_age_hours:.1f}h old")

            reason = (
                f"Built ${position.total_invested_usd:,.0f} position "
                f"via {position.trade_count} trades at avg {position.average_price:.1%} odds "
                f"({', '.join(reasons)})"
            )

            alert = AggregateWhaleAlert(
                user_address=address,
                position=position,
                profile=profile,
                detection_reason=reason
            )
            alerts.append(alert)

            self.logger.warning(
                f"AGGREGATE WHALE DETECTED: ${position.total_invested_usd:,.2f} by "
                f"{address[:10]}... in {position.market_title[:40]}..."
            )

        return alerts

    def run_aggregate_scan(self) -> List[AggregateWhaleAlert]:
        """
        Periodic scan for Aggregate Asymmetric Whales.

        This method:
        1. Finds accounts with recent significant activity
        2. Aggregates their trades by market
        3. Checks against AAW detection criteria
        4. Returns alerts for qualifying positions
        """
        if not self.config.ENABLE_AGGREGATE_DETECTION:
            return []

        alerts = []

        self.logger.info("Starting aggregate whale scan...")

        # Get accounts with recent activity
        active_accounts = self.get_recent_active_accounts()
        self.logger.info(f"Found {len(active_accounts)} accounts with significant activity")

        # Check each account
        for address in active_accounts:
            try:
                # Get account profile
                profile = self.client.get_account_profile(address)
                if profile is None:
                    continue

                # Get aggregated positions
                positions = self.get_account_positions(address)
                if not positions:
                    continue

                # Check for aggregate whale criteria
                account_alerts = self.check_aggregate_whale(address, positions, profile)
                for alert in account_alerts:
                    alerts.append(alert)
                    self.stats["aggregate_whales_detected"] += 1

                    # Send notification
                    if self.notifier.send_alert(alert):
                        self.stats["alerts_sent"] += 1

                # Rate limit between accounts
                time.sleep(self.config.RATE_LIMIT_DELAY)

            except Exception as e:
                self.logger.warning(f"Error checking account {address[:10]}...: {e}")
                continue

        self.last_aggregate_scan = datetime.now(timezone.utc)
        self.logger.info(
            f"Aggregate scan complete. Found {len(alerts)} aggregate whales."
        )

        return alerts

    def _should_run_aggregate_scan(self) -> bool:
        """Check if it's time to run an aggregate scan."""
        if not self.config.ENABLE_AGGREGATE_DETECTION:
            return False

        if self.last_aggregate_scan is None:
            return True

        elapsed = datetime.now(timezone.utc) - self.last_aggregate_scan
        interval = timedelta(minutes=self.config.AGGREGATE_SCAN_INTERVAL_MINUTES)

        return elapsed >= interval

    def run(self, max_iterations: Optional[int] = None):
        """
        Main monitoring loop.

        Args:
            max_iterations: If set, stop after this many polling cycles.
                          None means run indefinitely.
        """
        self._setup_logging()
        self.stats["start_time"] = datetime.now(timezone.utc)

        self.logger.info("=" * 60)
        self.logger.info("Polymarket Whale Monitor Starting")
        self.logger.info(f"Minimum trade value: ${self.config.MIN_TRADE_VALUE_USD:,.0f}")
        self.logger.info(f"New account threshold: <{self.config.MAX_HISTORICAL_TRADES} trades")
        self.logger.info(f"Account age threshold: <{self.config.NEW_ACCOUNT_HOURS} hours")
        self.logger.info(f"Poll interval: {self.config.POLL_INTERVAL_SECONDS} seconds")
        if self.config.ENABLE_AGGREGATE_DETECTION:
            self.logger.info("-" * 60)
            self.logger.info("Aggregate Detection: ENABLED")
            self.logger.info(f"  Min aggregate position: ${self.config.AGGREGATE_MIN_POSITION_USD:,.0f}")
            self.logger.info(f"  Asymmetric threshold: <{self.config.ASYMMETRIC_PRICE_THRESHOLD:.0%}")
            self.logger.info(f"  Scan interval: {self.config.AGGREGATE_SCAN_INTERVAL_MINUTES} minutes")
        self.logger.info("=" * 60)

        # Send startup notification
        self.notifier.send_startup_notification()

        iteration = 0
        try:
            while True:
                iteration += 1

                if max_iterations and iteration > max_iterations:
                    self.logger.info(f"Reached max iterations ({max_iterations}), stopping")
                    break

                self.logger.debug(f"Starting poll cycle #{iteration}")

                try:
                    alerts = self.run_single_poll()

                    if alerts:
                        self.logger.info(f"Poll #{iteration}: {len(alerts)} fresh whale alerts sent")
                    else:
                        self.logger.debug(f"Poll #{iteration}: No fresh whales detected")

                    # Run aggregate scan if it's time
                    if self._should_run_aggregate_scan():
                        try:
                            agg_alerts = self.run_aggregate_scan()
                            if agg_alerts:
                                self.logger.info(
                                    f"Aggregate scan: {len(agg_alerts)} aggregate whale alerts sent"
                                )
                        except Exception as e:
                            self.logger.error(f"Error in aggregate scan: {e}", exc_info=True)

                except Exception as e:
                    self.logger.error(f"Error in poll cycle: {e}", exc_info=True)

                # Wait before next poll
                if max_iterations is None or iteration < max_iterations:
                    time.sleep(self.config.POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            self.logger.info("Shutdown requested, stopping monitor...")

        # Print final statistics
        self._print_stats()

    def _print_stats(self):
        """Print monitoring statistics."""
        runtime = datetime.now(timezone.utc) - self.stats["start_time"]

        self.logger.info("=" * 60)
        self.logger.info("Monitoring Session Statistics")
        self.logger.info(f"Runtime: {runtime}")
        self.logger.info(f"Trades scanned: {self.stats['total_trades_scanned']}")
        self.logger.info(f"Large trades found: {self.stats['large_trades_found']}")
        self.logger.info(f"Fresh whales detected: {self.stats['fresh_whales_detected']}")
        self.logger.info(f"Aggregate whales detected: {self.stats['aggregate_whales_detected']}")
        self.logger.info(f"Total alerts sent: {self.stats['alerts_sent']}")
        self.logger.info("=" * 60)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """Main entry point for CLI execution."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Monitor Polymarket for Fresh Whale activity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Real-time monitoring
  python whale_monitor.py --webhook YOUR_DISCORD_WEBHOOK_URL

  # Monitor with custom thresholds
  python whale_monitor.py --min-value 50000 --max-trades 3

  # Run historical backtest (last 7 days)
  python whale_monitor.py backtest --days 7

  # Backtest with CSV export
  python whale_monitor.py backtest --days 14 --output whales.csv
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Monitor subcommand (default behavior)
    monitor_parser = subparsers.add_parser("monitor", help="Real-time monitoring (default)")
    monitor_parser.add_argument(
        "--webhook", "-w",
        help="Discord webhook URL (or set DISCORD_WEBHOOK_URL env var)",
        default=os.getenv("DISCORD_WEBHOOK_URL", "")
    )
    monitor_parser.add_argument(
        "--min-value", "-m",
        type=float,
        default=10000,
        help="Minimum trade value in USD to trigger alert (default: 10000)"
    )
    monitor_parser.add_argument(
        "--interval", "-i",
        type=int,
        default=60,
        help="Polling interval in seconds (default: 60)"
    )
    monitor_parser.add_argument(
        "--max-trades", "-t",
        type=int,
        default=5,
        help="Max historical trades for 'new account' (default: 5)"
    )
    monitor_parser.add_argument(
        "--account-hours", "-a",
        type=int,
        default=72,
        help="Account age threshold in hours (default: 72)"
    )
    monitor_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    monitor_parser.add_argument(
        "--test",
        action="store_true",
        help="Run a single poll cycle then exit (for testing)"
    )
    # Aggregate detection arguments
    monitor_parser.add_argument(
        "--aggregate-min",
        type=float,
        default=30000,
        help="Minimum aggregate position value in USD (default: 30000)"
    )
    monitor_parser.add_argument(
        "--asymmetric-price",
        type=float,
        default=0.30,
        help="Max average price for asymmetric detection (default: 0.30)"
    )
    monitor_parser.add_argument(
        "--aggregate-days",
        type=int,
        default=14,
        help="Days to look back for aggregate positions (default: 14)"
    )
    monitor_parser.add_argument(
        "--aggregate-interval",
        type=int,
        default=30,
        help="Aggregate scan interval in minutes (default: 30)"
    )
    monitor_parser.add_argument(
        "--no-aggregate",
        action="store_true",
        help="Disable aggregate whale detection"
    )

    # Backtest subcommand
    backtest_parser = subparsers.add_parser(
        "backtest",
        help="Run historical backtest for validation"
    )
    backtest_parser.add_argument(
        "--days", "-d",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)"
    )
    backtest_parser.add_argument(
        "--min-value", "-m",
        type=float,
        default=10000,
        help="Minimum trade value in USD (default: 10000)"
    )
    backtest_parser.add_argument(
        "--max-trades", "-t",
        type=int,
        default=5,
        help="Max prior trades for 'new account' (default: 5)"
    )
    backtest_parser.add_argument(
        "--account-hours", "-a",
        type=int,
        default=72,
        help="Account age threshold in hours (default: 72)"
    )
    backtest_parser.add_argument(
        "--output", "-o",
        type=str,
        help="Export Fresh Whale results to CSV file"
    )
    backtest_parser.add_argument(
        "--output-all",
        type=str,
        help="Export ALL analyzed trades to CSV file"
    )
    backtest_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    # Aggregate backtest arguments
    backtest_parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Run aggregate whale detection backtest"
    )
    backtest_parser.add_argument(
        "--aggregate-min",
        type=float,
        default=30000,
        help="Minimum aggregate position value (default: 30000)"
    )
    backtest_parser.add_argument(
        "--asymmetric-price",
        type=float,
        default=0.30,
        help="Max average price for asymmetric detection (default: 0.30)"
    )
    backtest_parser.add_argument(
        "--output-aggregate",
        type=str,
        help="Export aggregate whale results to CSV file"
    )

    # Add default args for backward compatibility (when no subcommand specified)
    parser.add_argument(
        "--webhook", "-w",
        help="Discord webhook URL (or set DISCORD_WEBHOOK_URL env var)",
        default=os.getenv("DISCORD_WEBHOOK_URL", "")
    )
    parser.add_argument(
        "--min-value", "-m",
        type=float,
        default=10000,
        help="Minimum trade value in USD to trigger alert (default: 10000)"
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=60,
        help="Polling interval in seconds (default: 60)"
    )
    parser.add_argument(
        "--max-trades", "-t",
        type=int,
        default=5,
        help="Max historical trades for 'new account' (default: 5)"
    )
    parser.add_argument(
        "--account-hours", "-a",
        type=int,
        default=72,
        help="Account age threshold in hours (default: 72)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a single poll cycle then exit (for testing)"
    )
    # Aggregate detection arguments (default parser)
    parser.add_argument(
        "--aggregate-min",
        type=float,
        default=30000,
        help="Minimum aggregate position value in USD (default: 30000)"
    )
    parser.add_argument(
        "--asymmetric-price",
        type=float,
        default=0.30,
        help="Max average price for asymmetric detection (default: 0.30)"
    )
    parser.add_argument(
        "--aggregate-days",
        type=int,
        default=14,
        help="Days to look back for aggregate positions (default: 14)"
    )
    parser.add_argument(
        "--aggregate-interval",
        type=int,
        default=30,
        help="Aggregate scan interval in minutes (default: 30)"
    )
    parser.add_argument(
        "--no-aggregate",
        action="store_true",
        help="Disable aggregate whale detection"
    )

    args = parser.parse_args()

    # Handle backtest command
    if args.command == "backtest":
        try:
            from backtest import (
                BacktestConfig, BacktestEngine, export_to_csv, export_all_trades_csv,
                AggregateBacktestConfig, run_aggregate_backtest, export_aggregate_csv
            )
        except ImportError as e:
            # Try basic import first
            try:
                from backtest import BacktestConfig, BacktestEngine, export_to_csv, export_all_trades_csv
            except ImportError:
                print("Error: backtest.py module not found. Make sure it's in the same directory.")
                sys.exit(1)

            # Aggregate imports failed - run basic backtest only
            if args.aggregate:
                print("Warning: Aggregate backtest not available. Running standard backtest.")
            config = BacktestConfig(
                LOOKBACK_DAYS=args.days,
                MIN_TRADE_VALUE_USD=args.min_value,
                MAX_HISTORICAL_TRADES=args.max_trades,
                NEW_ACCOUNT_HOURS=args.account_hours,
                LOG_LEVEL="DEBUG" if args.debug else "INFO"
            )
            engine = BacktestEngine(config)
            results = engine.run()
            results.print_summary()
            if args.output:
                export_to_csv(results, args.output)
            if args.output_all:
                export_all_trades_csv(results, args.output_all)
            return

        # Run aggregate backtest if requested
        if args.aggregate:
            agg_config = AggregateBacktestConfig(
                LOOKBACK_DAYS=args.days,
                AGGREGATE_MIN_POSITION_USD=args.aggregate_min,
                ASYMMETRIC_PRICE_THRESHOLD=args.asymmetric_price,
                LOG_LEVEL="DEBUG" if args.debug else "INFO"
            )
            agg_results = run_aggregate_backtest(agg_config)
            if args.output_aggregate:
                export_aggregate_csv(agg_results, args.output_aggregate)
            return

        # Standard fresh whale backtest
        config = BacktestConfig(
            LOOKBACK_DAYS=args.days,
            MIN_TRADE_VALUE_USD=args.min_value,
            MAX_HISTORICAL_TRADES=args.max_trades,
            NEW_ACCOUNT_HOURS=args.account_hours,
            LOG_LEVEL="DEBUG" if args.debug else "INFO"
        )

        engine = BacktestEngine(config)
        results = engine.run()
        results.print_summary()

        if args.output:
            export_to_csv(results, args.output)
        if args.output_all:
            export_all_trades_csv(results, args.output_all)

        return

    # Default: run real-time monitor
    # Get aggregate settings (handle both monitor subcommand and default)
    aggregate_min = getattr(args, 'aggregate_min', 30000)
    asymmetric_price = getattr(args, 'asymmetric_price', 0.30)
    aggregate_days = getattr(args, 'aggregate_days', 14)
    aggregate_interval = getattr(args, 'aggregate_interval', 30)
    no_aggregate = getattr(args, 'no_aggregate', False)

    config = Config(
        DISCORD_WEBHOOK_URL=args.webhook,
        MIN_TRADE_VALUE_USD=args.min_value,
        POLL_INTERVAL_SECONDS=args.interval,
        MAX_HISTORICAL_TRADES=args.max_trades,
        NEW_ACCOUNT_HOURS=args.account_hours,
        LOG_LEVEL="DEBUG" if args.debug else "INFO",
        # Aggregate detection settings
        AGGREGATE_MIN_POSITION_USD=aggregate_min,
        ASYMMETRIC_PRICE_THRESHOLD=asymmetric_price,
        AGGREGATE_LOOKBACK_DAYS=aggregate_days,
        AGGREGATE_SCAN_INTERVAL_MINUTES=aggregate_interval,
        ENABLE_AGGREGATE_DETECTION=not no_aggregate
    )

    monitor = WhaleMonitor(config)
    monitor.run(max_iterations=1 if args.test else None)


if __name__ == "__main__":
    main()
