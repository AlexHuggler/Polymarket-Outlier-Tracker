#!/usr/bin/env python3
"""
Polymarket Whale Monitor
========================

This script monitors Polymarket for "Fresh Whale" activity - detecting when
newly created accounts suddenly invest large sums (>$10,000 USD) into markets.

Data Source: Polymarket Subgraph (The Graph) via GraphQL
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
import hashlib

import requests

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class Config:
    """Configuration settings for the Whale Monitor."""

    # Polymarket Subgraph endpoint
    SUBGRAPH_URL: str = (
        "https://api.goldsky.com/api/public/"
        "project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-subgraph/prod/gn"
    )

    # Discord webhook URL (set via environment variable or directly)
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # Thresholds for "Fresh Whale" detection
    MIN_TRADE_VALUE_USD: float = 10_000.0  # Minimum trade value to trigger alert
    MAX_HISTORICAL_TRADES: int = 5  # Max trades for "new account"
    NEW_ACCOUNT_HOURS: int = 72  # Account age threshold in hours

    # Polling configuration
    POLL_INTERVAL_SECONDS: int = 60  # How often to check for new trades
    LOOKBACK_MINUTES: int = 5  # How far back to look for trades each poll

    # Aggregate detection thresholds (for detecting stealth accumulation)
    AGGREGATE_MIN_POSITION_USD: float = 30_000.0  # Min aggregate position to trigger
    ASYMMETRIC_PRICE_THRESHOLD: float = 0.30  # Price below 30% = asymmetric market
    AGGREGATE_LOOKBACK_DAYS: int = 14  # How far back to look for aggregate positions
    AGGREGATE_SCAN_INTERVAL_MINUTES: int = 30  # How often to run aggregate scan
    MAX_SINGLE_TRADE_FOR_AGGREGATE: float = 10_000.0  # Max single trade for stealth pattern
    ENABLE_AGGREGATE_DETECTION: bool = True  # Enable/disable aggregate detection

    # API settings
    REQUEST_TIMEOUT: int = 30  # HTTP request timeout in seconds
    MAX_RETRIES: int = 3  # Max retries for failed requests
    RETRY_DELAY: int = 5  # Delay between retries in seconds

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

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
class MarketPosition:
    """Aggregated position in a single market."""
    market_id: str
    market_title: str
    outcome: str  # "Yes" or "No"
    total_shares: float
    average_price: float
    total_invested_usd: float
    trade_count: int
    first_trade_timestamp: int
    last_trade_timestamp: int
    max_single_trade_usd: float  # Largest individual trade

    @property
    def is_asymmetric(self) -> bool:
        """Check if this is an asymmetric (low-odds) position."""
        return self.average_price < 0.30

    @property
    def time_span_hours(self) -> float:
        """Time span from first to last trade in hours."""
        if self.first_trade_timestamp and self.last_trade_timestamp:
            return (self.last_trade_timestamp - self.first_trade_timestamp) / 3600
        return 0


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

        return embed


@dataclass
class AggregateWhaleAlert:
    """Alert data for aggregate position detection (stealth accumulation)."""
    address: str
    position: MarketPosition
    profile: AccountProfile
    detection_reason: str
    alert_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_discord_embed(self) -> Dict[str, Any]:
        """Format alert as Discord embed with distinctive purple color."""
        # Purple color for aggregate alerts to distinguish from single-trade
        if self.position.total_invested_usd >= 100_000:
            color = 0x8B008B  # Dark magenta for very large
        elif self.position.total_invested_usd >= 50_000:
            color = 0x9B59B6  # Purple for large
        else:
            color = 0xAA88FF  # Light purple for threshold

        # Build Polymarket profile URL
        profile_url = f"https://polymarket.com/profile/{self.address}"

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

        # Format time span
        if self.position.time_span_hours < 1:
            span_str = f"{int(self.position.time_span_hours * 60)} minutes"
        elif self.position.time_span_hours < 24:
            span_str = f"{self.position.time_span_hours:.1f} hours"
        else:
            span_str = f"{self.position.time_span_hours / 24:.1f} days"

        embed = {
            "title": "Aggregate Position Detected!",
            "description": (
                f"Account accumulated a large position through multiple small trades "
                f"in a low-odds market."
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
                    "value": f"**{self.position.outcome}** @ avg {self.position.average_price:.1%}",
                    "inline": True
                },
                {
                    "name": "Total Invested",
                    "value": f"**${self.position.total_invested_usd:,.2f}**",
                    "inline": True
                },
                {
                    "name": "Total Shares",
                    "value": f"{self.position.total_shares:,.2f}",
                    "inline": True
                },
                {
                    "name": "Number of Trades",
                    "value": str(self.position.trade_count),
                    "inline": True
                },
                {
                    "name": "Largest Single Trade",
                    "value": f"${self.position.max_single_trade_usd:,.2f}",
                    "inline": True
                },
                {
                    "name": "Accumulation Period",
                    "value": span_str,
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
                    "inline": False
                },
                {
                    "name": "Wallet Address",
                    "value": f"`{self.address[:10]}...{self.address[-8:]}`",
                    "inline": False
                }
            ],
            "timestamp": self.alert_time.isoformat(),
            "footer": {
                "text": "Polymarket Whale Monitor - Aggregate Detection"
            },
            "url": profile_url
        }

        return embed


# =============================================================================
# GRAPHQL QUERIES
# =============================================================================

class PolymarketSubgraph:
    """Client for interacting with the Polymarket Subgraph."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)

    def _execute_query(
        self,
        query: str,
        variables: Optional[Dict] = None
    ) -> Optional[Dict[str, Any]]:
        """Execute a GraphQL query with retry logic."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.post(
                    self.config.SUBGRAPH_URL,
                    json=payload,
                    timeout=self.config.REQUEST_TIMEOUT,
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()

                result = response.json()

                if "errors" in result:
                    self.logger.error(f"GraphQL errors: {result['errors']}")
                    return None

                return result.get("data")

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

    def get_recent_trades(
        self,
        since_timestamp: int,
        min_value_usd: float = 0,
        first: int = 100
    ) -> List[Trade]:
        """
        Fetch recent trades from the subgraph.

        Note: The actual Polymarket subgraph schema may vary. This query
        is structured based on common subgraph patterns. You may need to
        adjust field names based on the actual schema.
        """
        # Query for recent trading activity
        # The actual schema fields may differ - this is a common pattern
        query = """
        query GetRecentTrades($since: BigInt!, $first: Int!) {
            trades(
                first: $first,
                orderBy: timestamp,
                orderDirection: desc,
                where: { timestamp_gte: $since }
            ) {
                id
                user {
                    id
                }
                market {
                    id
                    question
                }
                outcome
                amount
                price
                timestamp
                transactionHash
            }
        }
        """

        # Alternative query structure for position-based events
        position_query = """
        query GetRecentPositions($since: BigInt!, $first: Int!) {
            fpmmTrades(
                first: $first,
                orderBy: creationTimestamp,
                orderDirection: desc,
                where: { creationTimestamp_gte: $since }
            ) {
                id
                creator {
                    id
                }
                fpmm {
                    id
                    question
                }
                outcomeIndex
                collateralAmount
                outcomeTokensAmount
                creationTimestamp
                transactionHash
            }
        }
        """

        # Try the primary query structure first
        variables = {"since": str(since_timestamp), "first": first}
        data = self._execute_query(query, variables)

        trades = []

        if data and "trades" in data:
            for t in data["trades"]:
                try:
                    # Calculate trade value
                    amount = float(t.get("amount", 0))
                    price = float(t.get("price", 0))
                    value_usd = amount * price

                    # Filter by minimum value
                    if value_usd < min_value_usd:
                        continue

                    trade = Trade(
                        id=t["id"],
                        user_address=t["user"]["id"],
                        market_id=t["market"]["id"],
                        market_title=t["market"].get("question", "Unknown Market"),
                        outcome=t.get("outcome", "Unknown"),
                        amount=amount,
                        price=price,
                        value_usd=value_usd,
                        timestamp=int(t["timestamp"]),
                        tx_hash=t.get("transactionHash", "")
                    )
                    trades.append(trade)
                except (KeyError, ValueError, TypeError) as e:
                    self.logger.warning(f"Failed to parse trade: {e}")
                    continue

        # If primary query fails, try alternative structure
        if not trades:
            data = self._execute_query(position_query, variables)
            if data and "fpmmTrades" in data:
                for t in data["fpmmTrades"]:
                    try:
                        # Calculate value from collateral
                        collateral = float(t.get("collateralAmount", 0)) / 1e6  # USDC decimals
                        outcome_tokens = float(t.get("outcomeTokensAmount", 0)) / 1e18

                        # Calculate effective price
                        price = collateral / outcome_tokens if outcome_tokens > 0 else 0

                        if collateral < min_value_usd:
                            continue

                        outcome_index = int(t.get("outcomeIndex", 0))
                        outcome = "Yes" if outcome_index == 0 else "No"

                        trade = Trade(
                            id=t["id"],
                            user_address=t["creator"]["id"],
                            market_id=t["fpmm"]["id"],
                            market_title=t["fpmm"].get("question", "Unknown Market"),
                            outcome=outcome,
                            amount=outcome_tokens,
                            price=price,
                            value_usd=collateral,
                            timestamp=int(t["creationTimestamp"]),
                            tx_hash=t.get("transactionHash", "")
                        )
                        trades.append(trade)
                    except (KeyError, ValueError, TypeError) as e:
                        self.logger.warning(f"Failed to parse fpmm trade: {e}")
                        continue

        self.logger.info(f"Found {len(trades)} trades above ${min_value_usd:,.0f}")
        return trades

    def get_account_profile(self, address: str) -> Optional[AccountProfile]:
        """
        Fetch account history and profile information.

        This queries the user's trading history to determine:
        - Total number of trades
        - First trade timestamp (account age)
        - Total volume traded
        """
        query = """
        query GetAccountProfile($address: String!) {
            user(id: $address) {
                id
                trades(first: 1000, orderBy: timestamp, orderDirection: asc) {
                    id
                    timestamp
                    amount
                    price
                }
            }
        }
        """

        # Alternative query for fpmm-based schema
        alt_query = """
        query GetAccountProfile($address: String!) {
            account(id: $address) {
                id
                fpmmTrades(first: 1000, orderBy: creationTimestamp, orderDirection: asc) {
                    id
                    creationTimestamp
                    collateralAmount
                    fpmm {
                        id
                    }
                }
            }
        }
        """

        variables = {"address": address.lower()}
        data = self._execute_query(query, variables)

        if data and data.get("user"):
            user = data["user"]
            trades = user.get("trades", [])

            total_trades = len(trades)
            first_timestamp = int(trades[0]["timestamp"]) if trades else None

            # Calculate total volume
            total_volume = sum(
                float(t.get("amount", 0)) * float(t.get("price", 0))
                for t in trades
            )

            # Count unique markets
            markets = set()
            # Note: market info not available in this query structure

            return AccountProfile(
                address=address,
                total_trades=total_trades,
                first_trade_timestamp=first_timestamp,
                total_volume_usd=total_volume,
                markets_traded=len(markets)
            )

        # Try alternative query
        data = self._execute_query(alt_query, variables)

        if data and data.get("account"):
            account = data["account"]
            trades = account.get("fpmmTrades", [])

            total_trades = len(trades)
            first_timestamp = int(trades[0]["creationTimestamp"]) if trades else None

            # Calculate total volume from collateral
            total_volume = sum(
                float(t.get("collateralAmount", 0)) / 1e6
                for t in trades
            )

            # Count unique markets
            markets = set(t.get("fpmm", {}).get("id", "") for t in trades)

            return AccountProfile(
                address=address,
                total_trades=total_trades,
                first_trade_timestamp=first_timestamp,
                total_volume_usd=total_volume,
                markets_traded=len(markets)
            )

        # Return empty profile if account not found (truly new)
        return AccountProfile(
            address=address,
            total_trades=0,
            first_trade_timestamp=None,
            total_volume_usd=0,
            markets_traded=0
        )

    def get_account_positions(
        self,
        address: str,
        since_timestamp: int
    ) -> Dict[str, MarketPosition]:
        """
        Fetch all trades for an account since timestamp,
        aggregated by market into positions.

        Returns: {market_id: MarketPosition}
        """
        query = """
        query GetAccountRecentTrades($address: String!, $since: BigInt!) {
            user(id: $address) {
                trades(
                    first: 1000,
                    where: { timestamp_gte: $since },
                    orderBy: timestamp,
                    orderDirection: asc
                ) {
                    id
                    market {
                        id
                        question
                    }
                    outcome
                    amount
                    price
                    timestamp
                }
            }
        }
        """

        alt_query = """
        query GetAccountRecentTrades($address: String!, $since: BigInt!) {
            account(id: $address) {
                fpmmTrades(
                    first: 1000,
                    where: { creationTimestamp_gte: $since },
                    orderBy: creationTimestamp,
                    orderDirection: asc
                ) {
                    id
                    fpmm {
                        id
                        question
                    }
                    outcomeIndex
                    collateralAmount
                    outcomeTokensAmount
                    creationTimestamp
                }
            }
        }
        """

        variables = {"address": address.lower(), "since": str(since_timestamp)}
        data = self._execute_query(query, variables)

        positions: Dict[str, MarketPosition] = {}

        if data and data.get("user"):
            trades = data["user"].get("trades", [])
            positions = self._aggregate_trades_to_positions(trades, "primary")

        if not positions:
            data = self._execute_query(alt_query, variables)
            if data and data.get("account"):
                trades = data["account"].get("fpmmTrades", [])
                positions = self._aggregate_trades_to_positions(trades, "fpmm")

        self.logger.info(
            f"Found {len(positions)} market positions for {address[:10]}..."
        )
        return positions

    def _aggregate_trades_to_positions(
        self,
        trades: List[Dict],
        schema_type: str
    ) -> Dict[str, MarketPosition]:
        """Aggregate individual trades into market positions."""
        # Group trades by market+outcome
        position_data: Dict[str, Dict] = {}

        for t in trades:
            try:
                if schema_type == "primary":
                    market_id = t["market"]["id"]
                    market_title = t["market"].get("question", "Unknown Market")
                    outcome = t.get("outcome", "Unknown")
                    amount = float(t.get("amount", 0))
                    price = float(t.get("price", 0))
                    value_usd = amount * price
                    timestamp = int(t["timestamp"])
                else:  # fpmm schema
                    market_id = t["fpmm"]["id"]
                    market_title = t["fpmm"].get("question", "Unknown Market")
                    outcome_index = int(t.get("outcomeIndex", 0))
                    outcome = "Yes" if outcome_index == 0 else "No"
                    collateral = float(t.get("collateralAmount", 0)) / 1e6
                    outcome_tokens = float(t.get("outcomeTokensAmount", 0)) / 1e18
                    price = collateral / outcome_tokens if outcome_tokens > 0 else 0
                    amount = outcome_tokens
                    value_usd = collateral
                    timestamp = int(t["creationTimestamp"])

                # Key by market+outcome to track positions separately
                key = f"{market_id}:{outcome}"

                if key not in position_data:
                    position_data[key] = {
                        "market_id": market_id,
                        "market_title": market_title,
                        "outcome": outcome,
                        "total_shares": 0,
                        "total_invested_usd": 0,
                        "trade_count": 0,
                        "first_trade_timestamp": timestamp,
                        "last_trade_timestamp": timestamp,
                        "max_single_trade_usd": 0,
                        "prices": [],
                    }

                pos = position_data[key]
                pos["total_shares"] += amount
                pos["total_invested_usd"] += value_usd
                pos["trade_count"] += 1
                pos["last_trade_timestamp"] = max(pos["last_trade_timestamp"], timestamp)
                pos["first_trade_timestamp"] = min(pos["first_trade_timestamp"], timestamp)
                pos["max_single_trade_usd"] = max(pos["max_single_trade_usd"], value_usd)
                pos["prices"].append((value_usd, price))  # For weighted average

            except (KeyError, ValueError, TypeError) as e:
                self.logger.warning(f"Failed to parse trade for aggregation: {e}")
                continue

        # Convert to MarketPosition objects
        positions: Dict[str, MarketPosition] = {}
        for key, pos in position_data.items():
            # Calculate weighted average price
            total_value = sum(p[0] for p in pos["prices"])
            if total_value > 0:
                avg_price = sum(p[0] * p[1] for p in pos["prices"]) / total_value
            else:
                avg_price = 0

            positions[key] = MarketPosition(
                market_id=pos["market_id"],
                market_title=pos["market_title"],
                outcome=pos["outcome"],
                total_shares=pos["total_shares"],
                average_price=avg_price,
                total_invested_usd=pos["total_invested_usd"],
                trade_count=pos["trade_count"],
                first_trade_timestamp=pos["first_trade_timestamp"],
                last_trade_timestamp=pos["last_trade_timestamp"],
                max_single_trade_usd=pos["max_single_trade_usd"],
            )

        return positions

    def get_recent_active_accounts(
        self,
        since_timestamp: int,
        min_trades: int = 2,
        first: int = 1000
    ) -> List[str]:
        """
        Get list of accounts that have been active since timestamp.
        Returns deduplicated list of addresses with at least min_trades.
        """
        query = """
        query GetRecentTrades($since: BigInt!, $first: Int!) {
            trades(
                first: $first,
                where: { timestamp_gte: $since },
                orderBy: timestamp,
                orderDirection: desc
            ) {
                user {
                    id
                }
            }
        }
        """

        alt_query = """
        query GetRecentTrades($since: BigInt!, $first: Int!) {
            fpmmTrades(
                first: $first,
                where: { creationTimestamp_gte: $since },
                orderBy: creationTimestamp,
                orderDirection: desc
            ) {
                creator {
                    id
                }
            }
        }
        """

        variables = {"since": str(since_timestamp), "first": first}
        data = self._execute_query(query, variables)

        # Count trades per account
        account_trades: Dict[str, int] = {}

        if data and "trades" in data:
            for t in data["trades"]:
                addr = t.get("user", {}).get("id", "")
                if addr:
                    account_trades[addr] = account_trades.get(addr, 0) + 1

        if not account_trades:
            data = self._execute_query(alt_query, variables)
            if data and "fpmmTrades" in data:
                for t in data["fpmmTrades"]:
                    addr = t.get("creator", {}).get("id", "")
                    if addr:
                        account_trades[addr] = account_trades.get(addr, 0) + 1

        # Filter to accounts with at least min_trades
        active_accounts = [
            addr for addr, count in account_trades.items()
            if count >= min_trades
        ]

        self.logger.info(
            f"Found {len(active_accounts)} accounts with >={min_trades} trades"
        )
        return active_accounts


# =============================================================================
# DISCORD ALERTING
# =============================================================================

class DiscordNotifier:
    """Sends alerts to Discord via webhook."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()

    def send_alert(self, alert: FreshWhaleAlert) -> bool:
        """Send a Fresh Whale alert to Discord."""
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

    def send_aggregate_alert(self, alert: AggregateWhaleAlert) -> bool:
        """Send an Aggregate Whale alert to Discord."""
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
                    f"Aggregate alert sent for ${alert.position.total_invested_usd:,.2f} "
                    f"position by {alert.address[:10]}..."
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

        description_lines = [
            f"Monitoring for trades above **${self.config.MIN_TRADE_VALUE_USD:,.0f}**",
            f"Polling every **{self.config.POLL_INTERVAL_SECONDS}** seconds",
        ]

        if self.config.ENABLE_AGGREGATE_DETECTION:
            description_lines.extend([
                "",
                "**Aggregate Detection Enabled:**",
                f"• Min position: **${self.config.AGGREGATE_MIN_POSITION_USD:,.0f}**",
                f"• Asymmetric threshold: **{self.config.ASYMMETRIC_PRICE_THRESHOLD:.0%}**",
                f"• Lookback: **{self.config.AGGREGATE_LOOKBACK_DAYS}** days",
                f"• Scan interval: **{self.config.AGGREGATE_SCAN_INTERVAL_MINUTES}** min",
            ])

        payload = {
            "username": "Polymarket Whale Monitor",
            "embeds": [{
                "title": "Whale Monitor Started",
                "description": "\n".join(description_lines),
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

        self.subgraph = PolymarketSubgraph(self.config)
        self.notifier = DiscordNotifier(self.config)
        self.logger = logging.getLogger(self.__class__.__name__)

        # Track processed trades to avoid duplicate alerts
        self.processed_trade_ids: Set[str] = set()

        # Track alerted aggregate positions to avoid spam
        # Key format: f"{address}:{market_id}:{outcome}"
        self.alerted_aggregate_positions: Set[str] = set()

        # Track last aggregate scan time
        self.last_aggregate_scan: float = 0

        # Statistics
        self.stats = {
            "total_trades_scanned": 0,
            "large_trades_found": 0,
            "fresh_whales_detected": 0,
            "aggregate_whales_detected": 0,
            "alerts_sent": 0,
            "aggregate_alerts_sent": 0,
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
        profile = self.subgraph.get_account_profile(trade.user_address)

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
        trades = self.subgraph.get_recent_trades(
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

    def check_aggregate_whale(
        self,
        address: str,
        positions: Dict[str, MarketPosition]
    ) -> List[AggregateWhaleAlert]:
        """
        Check if an account has accumulated large positions through small trades.

        Detection criteria:
        1. Has position > AGGREGATE_MIN_POSITION_USD in single market
        2. Average price < ASYMMETRIC_PRICE_THRESHOLD (low-odds bet)
        3. No single trade > MAX_SINGLE_TRADE_FOR_AGGREGATE (stealth pattern)
        4. Account is fresh (< MAX_HISTORICAL_TRADES or < NEW_ACCOUNT_HOURS)
        """
        alerts = []

        for key, position in positions.items():
            # Skip if already alerted for this position
            alert_key = f"{address}:{key}"
            if alert_key in self.alerted_aggregate_positions:
                continue

            # Check aggregate value threshold
            if position.total_invested_usd < self.config.AGGREGATE_MIN_POSITION_USD:
                continue

            # Check if asymmetric market (low odds)
            if position.average_price >= self.config.ASYMMETRIC_PRICE_THRESHOLD:
                continue

            # Check if stealth pattern (no single large trade)
            if position.max_single_trade_usd >= self.config.MAX_SINGLE_TRADE_FOR_AGGREGATE:
                continue

            # Must have multiple trades (not just one large trade split)
            if position.trade_count < 2:
                continue

            # Fetch account profile
            profile = self.subgraph.get_account_profile(address)
            if profile is None:
                continue

            # Check if account qualifies as fresh
            if not profile.is_fresh_whale(self.config):
                self.logger.debug(
                    f"Account {address[:10]}... has {profile.total_trades} trades, "
                    f"not fresh for aggregate detection"
                )
                continue

            # Build detection reason
            reason = (
                f"Accumulated ${position.total_invested_usd:,.0f} through "
                f"{position.trade_count} trades at avg {position.average_price:.1%} "
                f"(max single: ${position.max_single_trade_usd:,.0f})"
            )

            alert = AggregateWhaleAlert(
                address=address,
                position=position,
                profile=profile,
                detection_reason=reason
            )

            alerts.append(alert)
            self.alerted_aggregate_positions.add(alert_key)
            self.stats["aggregate_whales_detected"] += 1

            self.logger.warning(
                f"AGGREGATE WHALE DETECTED: ${position.total_invested_usd:,.2f} by "
                f"{address[:10]}... in '{position.market_title[:40]}...' - {reason}"
            )

        return alerts

    def run_aggregate_scan(self) -> List[AggregateWhaleAlert]:
        """
        Scan for aggregate whale patterns.

        1. Get list of recently active accounts
        2. For each account, compute market positions
        3. Check each position against aggregate criteria
        4. Return alerts for any matches
        """
        if not self.config.ENABLE_AGGREGATE_DETECTION:
            return []

        self.logger.info("Starting aggregate whale scan...")
        alerts = []

        # Calculate lookback timestamp
        lookback_seconds = self.config.AGGREGATE_LOOKBACK_DAYS * 24 * 3600
        since_timestamp = int(time.time()) - lookback_seconds

        # Get recently active accounts (with at least 2 trades)
        active_accounts = self.subgraph.get_recent_active_accounts(
            since_timestamp=since_timestamp,
            min_trades=2
        )

        self.logger.info(f"Scanning {len(active_accounts)} active accounts for aggregate patterns")

        for address in active_accounts:
            try:
                # Get positions for this account
                positions = self.subgraph.get_account_positions(
                    address=address,
                    since_timestamp=since_timestamp
                )

                # Check for aggregate whale patterns
                account_alerts = self.check_aggregate_whale(address, positions)

                for alert in account_alerts:
                    alerts.append(alert)

                    # Send notification
                    if self.notifier.send_aggregate_alert(alert):
                        self.stats["aggregate_alerts_sent"] += 1

            except Exception as e:
                self.logger.warning(f"Error processing account {address[:10]}...: {e}")
                continue

        self.logger.info(
            f"Aggregate scan complete: {len(alerts)} alerts generated"
        )
        return alerts

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
            self.logger.info(f"Aggregate detection: ENABLED")
            self.logger.info(f"  Min aggregate position: ${self.config.AGGREGATE_MIN_POSITION_USD:,.0f}")
            self.logger.info(f"  Asymmetric threshold: {self.config.ASYMMETRIC_PRICE_THRESHOLD:.0%}")
            self.logger.info(f"  Lookback period: {self.config.AGGREGATE_LOOKBACK_DAYS} days")
            self.logger.info(f"  Scan interval: {self.config.AGGREGATE_SCAN_INTERVAL_MINUTES} minutes")
        else:
            self.logger.info(f"Aggregate detection: DISABLED")
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
                    # Run single-trade detection
                    alerts = self.run_single_poll()

                    if alerts:
                        self.logger.info(f"Poll #{iteration}: {len(alerts)} alerts sent")
                    else:
                        self.logger.debug(f"Poll #{iteration}: No fresh whales detected")

                    # Check if it's time for aggregate scan
                    if self.config.ENABLE_AGGREGATE_DETECTION:
                        now = time.time()
                        scan_interval = self.config.AGGREGATE_SCAN_INTERVAL_MINUTES * 60
                        if now - self.last_aggregate_scan >= scan_interval:
                            self.logger.info("Running periodic aggregate scan...")
                            aggregate_alerts = self.run_aggregate_scan()
                            if aggregate_alerts:
                                self.logger.info(
                                    f"Aggregate scan: {len(aggregate_alerts)} alerts sent"
                                )
                            self.last_aggregate_scan = now

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
        self.logger.info(f"Alerts sent: {self.stats['alerts_sent']}")
        if self.config.ENABLE_AGGREGATE_DETECTION:
            self.logger.info(f"Aggregate whales detected: {self.stats['aggregate_whales_detected']}")
            self.logger.info(f"Aggregate alerts sent: {self.stats['aggregate_alerts_sent']}")
        self.logger.info("=" * 60)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """Main entry point for CLI execution."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Monitor Polymarket for Fresh Whale activity"
    )
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

    # Aggregate detection arguments
    parser.add_argument(
        "--aggregate-min",
        type=float,
        default=30000,
        help="Minimum aggregate position in USD to trigger alert (default: 30000)"
    )
    parser.add_argument(
        "--asymmetric-price",
        type=float,
        default=0.30,
        help="Price threshold for asymmetric markets (default: 0.30 = 30%%)"
    )
    parser.add_argument(
        "--aggregate-days",
        type=int,
        default=14,
        help="Lookback period for aggregate detection in days (default: 14)"
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
        help="Disable aggregate detection"
    )

    args = parser.parse_args()

    # Build configuration
    config = Config(
        DISCORD_WEBHOOK_URL=args.webhook,
        MIN_TRADE_VALUE_USD=args.min_value,
        POLL_INTERVAL_SECONDS=args.interval,
        MAX_HISTORICAL_TRADES=args.max_trades,
        NEW_ACCOUNT_HOURS=args.account_hours,
        LOG_LEVEL="DEBUG" if args.debug else "INFO",
        # Aggregate detection config
        AGGREGATE_MIN_POSITION_USD=args.aggregate_min,
        ASYMMETRIC_PRICE_THRESHOLD=args.asymmetric_price,
        AGGREGATE_LOOKBACK_DAYS=args.aggregate_days,
        AGGREGATE_SCAN_INTERVAL_MINUTES=args.aggregate_interval,
        ENABLE_AGGREGATE_DETECTION=not args.no_aggregate,
    )

    # Run monitor
    monitor = WhaleMonitor(config)
    monitor.run(max_iterations=1 if args.test else None)


if __name__ == "__main__":
    main()
