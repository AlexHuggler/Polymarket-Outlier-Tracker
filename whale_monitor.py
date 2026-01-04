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

        self.subgraph = PolymarketSubgraph(self.config)
        self.notifier = DiscordNotifier(self.config)
        self.logger = logging.getLogger(self.__class__.__name__)

        # Track processed trades to avoid duplicate alerts
        self.processed_trade_ids: Set[str] = set()

        # Statistics
        self.stats = {
            "total_trades_scanned": 0,
            "large_trades_found": 0,
            "fresh_whales_detected": 0,
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
                        self.logger.info(f"Poll #{iteration}: {len(alerts)} alerts sent")
                    else:
                        self.logger.debug(f"Poll #{iteration}: No fresh whales detected")

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

    args = parser.parse_args()

    # Handle backtest command
    if args.command == "backtest":
        try:
            from backtest import BacktestConfig, BacktestEngine, export_to_csv, export_all_trades_csv
        except ImportError:
            print("Error: backtest.py module not found. Make sure it's in the same directory.")
            sys.exit(1)

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
    config = Config(
        DISCORD_WEBHOOK_URL=args.webhook,
        MIN_TRADE_VALUE_USD=args.min_value,
        POLL_INTERVAL_SECONDS=args.interval,
        MAX_HISTORICAL_TRADES=args.max_trades,
        NEW_ACCOUNT_HOURS=args.account_hours,
        LOG_LEVEL="DEBUG" if args.debug else "INFO"
    )

    monitor = WhaleMonitor(config)
    monitor.run(max_iterations=1 if args.test else None)


if __name__ == "__main__":
    main()
