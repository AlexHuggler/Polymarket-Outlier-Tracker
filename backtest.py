#!/usr/bin/env python3
"""
Polymarket Whale Monitor - Historical Backtest Module
======================================================

This module provides historical analysis capabilities to validate the
Fresh Whale detection logic by looking back at past trades.

Usage:
    python backtest.py --days 7 --min-value 10000 --output results.csv

Features:
    - Fetch historical trades over configurable time periods
    - Analyze account state AT THE TIME of each trade (not current state)
    - Generate detailed reports with all detected Fresh Whales
    - Export results to CSV for further analysis
    - Rate limiting and pagination for large datasets
"""

import os
import sys
import time
import json
import logging
import csv
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any, Set, Tuple
from dataclasses import dataclass, field, asdict
from collections import defaultdict

import requests

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BacktestConfig:
    """Configuration for historical backtest."""

    # Polymarket Subgraph endpoint
    SUBGRAPH_URL: str = (
        "https://api.goldsky.com/api/public/"
        "project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-subgraph/prod/gn"
    )

    # Detection thresholds
    MIN_TRADE_VALUE_USD: float = 10_000.0
    MAX_HISTORICAL_TRADES: int = 5
    NEW_ACCOUNT_HOURS: int = 72

    # Backtest parameters
    LOOKBACK_DAYS: int = 7
    BATCH_SIZE: int = 100  # Trades per query
    MAX_TRADES_TO_ANALYZE: int = 10000  # Safety limit

    # API settings
    REQUEST_TIMEOUT: int = 30
    MAX_RETRIES: int = 3
    RETRY_DELAY: int = 5
    RATE_LIMIT_DELAY: float = 0.5  # Delay between API calls

    # Logging
    LOG_LEVEL: str = "INFO"


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class HistoricalTrade:
    """A trade with point-in-time account analysis."""
    id: str
    user_address: str
    market_id: str
    market_title: str
    outcome: str
    amount: float
    price: float
    value_usd: float
    timestamp: int
    tx_hash: str

    # Account state AT THE TIME of this trade
    account_trades_before: int  # Number of trades BEFORE this one
    account_first_trade_ts: Optional[int]  # First trade timestamp
    account_age_hours_at_trade: Optional[float]  # Age when trade occurred

    # Detection result
    is_fresh_whale: bool
    detection_reason: str

    @property
    def formatted_time(self) -> str:
        return datetime.fromtimestamp(
            self.timestamp, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

    @property
    def polymarket_profile_url(self) -> str:
        return f"https://polymarket.com/profile/{self.user_address}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for CSV export."""
        return {
            "timestamp": self.formatted_time,
            "unix_timestamp": self.timestamp,
            "value_usd": self.value_usd,
            "market_title": self.market_title,
            "outcome": self.outcome,
            "price": self.price,
            "shares": self.amount,
            "user_address": self.user_address,
            "account_trades_before": self.account_trades_before,
            "account_age_hours": self.account_age_hours_at_trade,
            "is_fresh_whale": self.is_fresh_whale,
            "detection_reason": self.detection_reason,
            "tx_hash": self.tx_hash,
            "profile_url": self.polymarket_profile_url
        }


@dataclass
class BacktestResults:
    """Summary of backtest results."""
    start_time: datetime
    end_time: datetime
    config: BacktestConfig

    total_trades_fetched: int = 0
    large_trades_analyzed: int = 0
    fresh_whales_detected: int = 0

    fresh_whale_trades: List[HistoricalTrade] = field(default_factory=list)
    all_large_trades: List[HistoricalTrade] = field(default_factory=list)

    # Analytics
    unique_whale_addresses: Set[str] = field(default_factory=set)
    total_whale_volume_usd: float = 0.0
    trades_by_day: Dict[str, int] = field(default_factory=dict)
    whales_by_market: Dict[str, int] = field(default_factory=dict)

    def add_whale(self, trade: HistoricalTrade):
        """Add a detected fresh whale trade."""
        self.fresh_whale_trades.append(trade)
        self.fresh_whales_detected += 1
        self.unique_whale_addresses.add(trade.user_address)
        self.total_whale_volume_usd += trade.value_usd

        # Track by day
        day_key = datetime.fromtimestamp(
            trade.timestamp, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        self.trades_by_day[day_key] = self.trades_by_day.get(day_key, 0) + 1

        # Track by market
        market_key = trade.market_title[:50]
        self.whales_by_market[market_key] = self.whales_by_market.get(market_key, 0) + 1

    def print_summary(self):
        """Print a summary of backtest results."""
        print("\n" + "=" * 70)
        print("BACKTEST RESULTS SUMMARY")
        print("=" * 70)
        print(f"Period: {self.start_time.strftime('%Y-%m-%d')} to {self.end_time.strftime('%Y-%m-%d')}")
        print(f"Minimum trade value: ${self.config.MIN_TRADE_VALUE_USD:,.0f}")
        print(f"Fresh account criteria: <{self.config.MAX_HISTORICAL_TRADES} trades OR <{self.config.NEW_ACCOUNT_HOURS}h old")
        print("-" * 70)
        print(f"Total trades fetched: {self.total_trades_fetched:,}")
        print(f"Large trades analyzed: {self.large_trades_analyzed:,}")
        print(f"Fresh Whales detected: {self.fresh_whales_detected:,}")
        print(f"Unique whale addresses: {len(self.unique_whale_addresses):,}")
        print(f"Total whale volume: ${self.total_whale_volume_usd:,.2f}")

        if self.fresh_whale_trades:
            avg_trade = self.total_whale_volume_usd / self.fresh_whales_detected
            print(f"Average whale trade size: ${avg_trade:,.2f}")

            # Top trades
            print("\n" + "-" * 70)
            print("TOP 10 LARGEST FRESH WHALE TRADES:")
            print("-" * 70)
            sorted_trades = sorted(
                self.fresh_whale_trades,
                key=lambda t: t.value_usd,
                reverse=True
            )[:10]

            for i, trade in enumerate(sorted_trades, 1):
                print(f"\n{i}. ${trade.value_usd:,.2f} - {trade.formatted_time}")
                print(f"   Market: {trade.market_title[:60]}...")
                print(f"   Position: {trade.outcome} @ ${trade.price:.3f}")
                print(f"   Reason: {trade.detection_reason}")
                print(f"   Wallet: {trade.user_address[:12]}...{trade.user_address[-8:]}")

            # Trades by day
            if self.trades_by_day:
                print("\n" + "-" * 70)
                print("FRESH WHALES BY DAY:")
                print("-" * 70)
                for day in sorted(self.trades_by_day.keys()):
                    count = self.trades_by_day[day]
                    bar = "#" * min(count, 50)
                    print(f"{day}: {bar} ({count})")

        print("\n" + "=" * 70)


# =============================================================================
# SUBGRAPH CLIENT WITH HISTORICAL QUERIES
# =============================================================================

class HistoricalSubgraphClient:
    """Client for fetching historical data from Polymarket Subgraph."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)

        # Cache for account profiles to reduce API calls
        self._account_cache: Dict[str, List[Dict]] = {}

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

    def get_historical_trades(
        self,
        start_timestamp: int,
        end_timestamp: int,
        min_value_usd: float = 0
    ) -> List[Dict[str, Any]]:
        """
        Fetch all trades within a time range using pagination.

        Returns raw trade data for processing.
        """
        all_trades = []
        last_timestamp = end_timestamp
        last_id = ""

        # Query for trades - try both schema variants
        query_v1 = """
        query GetHistoricalTrades($start: BigInt!, $end: BigInt!, $first: Int!, $lastId: String!) {
            trades(
                first: $first,
                orderBy: timestamp,
                orderDirection: desc,
                where: {
                    timestamp_gte: $start,
                    timestamp_lte: $end,
                    id_lt: $lastId
                }
            ) {
                id
                user { id }
                market { id question }
                outcome
                amount
                price
                timestamp
                transactionHash
            }
        }
        """

        query_v1_initial = """
        query GetHistoricalTrades($start: BigInt!, $end: BigInt!, $first: Int!) {
            trades(
                first: $first,
                orderBy: timestamp,
                orderDirection: desc,
                where: {
                    timestamp_gte: $start,
                    timestamp_lte: $end
                }
            ) {
                id
                user { id }
                market { id question }
                outcome
                amount
                price
                timestamp
                transactionHash
            }
        }
        """

        # Alternative query for fpmm-based schema
        query_v2 = """
        query GetHistoricalTrades($start: BigInt!, $end: BigInt!, $first: Int!, $skip: Int!) {
            fpmmTrades(
                first: $first,
                skip: $skip,
                orderBy: creationTimestamp,
                orderDirection: desc,
                where: {
                    creationTimestamp_gte: $start,
                    creationTimestamp_lte: $end
                }
            ) {
                id
                creator { id }
                fpmm { id question }
                outcomeIndex
                collateralAmount
                outcomeTokensAmount
                creationTimestamp
                transactionHash
            }
        }
        """

        self.logger.info(f"Fetching trades from {start_timestamp} to {end_timestamp}...")

        # Try v1 schema first
        variables = {
            "start": str(start_timestamp),
            "end": str(end_timestamp),
            "first": self.config.BATCH_SIZE
        }

        data = self._execute_query(query_v1_initial, variables)

        if data and "trades" in data and data["trades"]:
            # Use v1 schema with pagination
            self.logger.info("Using trades schema (v1)")
            trades = data["trades"]
            all_trades.extend(trades)

            while len(trades) == self.config.BATCH_SIZE:
                if len(all_trades) >= self.config.MAX_TRADES_TO_ANALYZE:
                    self.logger.warning(f"Reached max trades limit ({self.config.MAX_TRADES_TO_ANALYZE})")
                    break

                last_id = trades[-1]["id"]
                variables["lastId"] = last_id

                time.sleep(self.config.RATE_LIMIT_DELAY)
                data = self._execute_query(query_v1, variables)

                if not data or "trades" not in data:
                    break

                trades = data["trades"]
                all_trades.extend(trades)
                self.logger.debug(f"Fetched {len(all_trades)} trades so far...")

            # Convert to standard format
            return self._normalize_v1_trades(all_trades, min_value_usd)

        # Try v2 schema (fpmmTrades)
        skip = 0
        variables = {
            "start": str(start_timestamp),
            "end": str(end_timestamp),
            "first": self.config.BATCH_SIZE,
            "skip": skip
        }

        data = self._execute_query(query_v2, variables)

        if data and "fpmmTrades" in data:
            self.logger.info("Using fpmmTrades schema (v2)")

            while True:
                if not data or "fpmmTrades" not in data:
                    break

                trades = data["fpmmTrades"]
                if not trades:
                    break

                all_trades.extend(trades)

                if len(trades) < self.config.BATCH_SIZE:
                    break

                if len(all_trades) >= self.config.MAX_TRADES_TO_ANALYZE:
                    self.logger.warning(f"Reached max trades limit ({self.config.MAX_TRADES_TO_ANALYZE})")
                    break

                skip += self.config.BATCH_SIZE
                variables["skip"] = skip

                time.sleep(self.config.RATE_LIMIT_DELAY)
                data = self._execute_query(query_v2, variables)

                self.logger.debug(f"Fetched {len(all_trades)} trades so far...")

            return self._normalize_v2_trades(all_trades, min_value_usd)

        self.logger.warning("No trades found with either schema")
        return []

    def _normalize_v1_trades(
        self,
        trades: List[Dict],
        min_value_usd: float
    ) -> List[Dict[str, Any]]:
        """Normalize v1 schema trades to standard format."""
        normalized = []

        for t in trades:
            try:
                amount = float(t.get("amount", 0))
                price = float(t.get("price", 0))
                value_usd = amount * price

                if value_usd < min_value_usd:
                    continue

                normalized.append({
                    "id": t["id"],
                    "user_address": t["user"]["id"],
                    "market_id": t["market"]["id"],
                    "market_title": t["market"].get("question", "Unknown Market"),
                    "outcome": t.get("outcome", "Unknown"),
                    "amount": amount,
                    "price": price,
                    "value_usd": value_usd,
                    "timestamp": int(t["timestamp"]),
                    "tx_hash": t.get("transactionHash", "")
                })
            except (KeyError, ValueError, TypeError) as e:
                self.logger.debug(f"Failed to parse trade: {e}")
                continue

        return normalized

    def _normalize_v2_trades(
        self,
        trades: List[Dict],
        min_value_usd: float
    ) -> List[Dict[str, Any]]:
        """Normalize v2 schema (fpmmTrades) to standard format."""
        normalized = []

        for t in trades:
            try:
                # USDC has 6 decimals
                collateral = float(t.get("collateralAmount", 0)) / 1e6
                outcome_tokens = float(t.get("outcomeTokensAmount", 0)) / 1e18

                if collateral < min_value_usd:
                    continue

                price = collateral / outcome_tokens if outcome_tokens > 0 else 0
                outcome_index = int(t.get("outcomeIndex", 0))
                outcome = "Yes" if outcome_index == 0 else "No"

                normalized.append({
                    "id": t["id"],
                    "user_address": t["creator"]["id"],
                    "market_id": t["fpmm"]["id"],
                    "market_title": t["fpmm"].get("question", "Unknown Market"),
                    "outcome": outcome,
                    "amount": outcome_tokens,
                    "price": price,
                    "value_usd": collateral,
                    "timestamp": int(t["creationTimestamp"]),
                    "tx_hash": t.get("transactionHash", "")
                })
            except (KeyError, ValueError, TypeError) as e:
                self.logger.debug(f"Failed to parse fpmm trade: {e}")
                continue

        return normalized

    def get_account_trades_before(
        self,
        address: str,
        before_timestamp: int
    ) -> Tuple[int, Optional[int]]:
        """
        Get the number of trades an account had BEFORE a specific timestamp.

        This is crucial for historical analysis - we need to know the account
        state at the time of the trade, not the current state.

        Returns: (trade_count_before, first_trade_timestamp)
        """
        address = address.lower()

        # Check cache first
        if address in self._account_cache:
            trades = self._account_cache[address]
        else:
            trades = self._fetch_all_account_trades(address)
            self._account_cache[address] = trades

        if not trades:
            return 0, None

        # Count trades before the given timestamp
        trades_before = [t for t in trades if t["timestamp"] < before_timestamp]

        first_timestamp = trades[0]["timestamp"] if trades else None

        return len(trades_before), first_timestamp

    def _fetch_all_account_trades(self, address: str) -> List[Dict]:
        """Fetch all trades for an account."""
        query_v1 = """
        query GetAccountTrades($address: String!) {
            user(id: $address) {
                id
                trades(first: 1000, orderBy: timestamp, orderDirection: asc) {
                    id
                    timestamp
                }
            }
        }
        """

        query_v2 = """
        query GetAccountTrades($address: String!) {
            account(id: $address) {
                id
                fpmmTrades(first: 1000, orderBy: creationTimestamp, orderDirection: asc) {
                    id
                    creationTimestamp
                }
            }
        }
        """

        time.sleep(self.config.RATE_LIMIT_DELAY)

        # Try v1
        data = self._execute_query(query_v1, {"address": address})
        if data and data.get("user"):
            trades = data["user"].get("trades", [])
            return [{"id": t["id"], "timestamp": int(t["timestamp"])} for t in trades]

        # Try v2
        data = self._execute_query(query_v2, {"address": address})
        if data and data.get("account"):
            trades = data["account"].get("fpmmTrades", [])
            return [{"id": t["id"], "timestamp": int(t["creationTimestamp"])} for t in trades]

        return []


# =============================================================================
# BACKTEST ENGINE
# =============================================================================

class BacktestEngine:
    """Engine for running historical backtest analysis."""

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()
        self.client = HistoricalSubgraphClient(self.config)
        self.logger = logging.getLogger(self.__class__.__name__)

    def _setup_logging(self):
        """Configure logging."""
        log_level = getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO)
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

    def run(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> BacktestResults:
        """
        Run the historical backtest.

        Args:
            start_date: Start of analysis period (default: LOOKBACK_DAYS ago)
            end_date: End of analysis period (default: now)

        Returns:
            BacktestResults with all detected fresh whales
        """
        self._setup_logging()

        # Set time range
        if end_date is None:
            end_date = datetime.now(timezone.utc)
        if start_date is None:
            start_date = end_date - timedelta(days=self.config.LOOKBACK_DAYS)

        start_ts = int(start_date.timestamp())
        end_ts = int(end_date.timestamp())

        results = BacktestResults(
            start_time=start_date,
            end_time=end_date,
            config=self.config
        )

        self.logger.info("=" * 60)
        self.logger.info("Starting Historical Backtest")
        self.logger.info(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        self.logger.info(f"Minimum trade value: ${self.config.MIN_TRADE_VALUE_USD:,.0f}")
        self.logger.info("=" * 60)

        # Fetch historical trades
        self.logger.info("Fetching historical trades...")
        trades = self.client.get_historical_trades(
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            min_value_usd=self.config.MIN_TRADE_VALUE_USD
        )

        results.total_trades_fetched = len(trades)
        self.logger.info(f"Found {len(trades)} trades above ${self.config.MIN_TRADE_VALUE_USD:,.0f}")

        # Analyze each trade
        self.logger.info("Analyzing trades for Fresh Whale patterns...")

        for i, trade_data in enumerate(trades):
            if (i + 1) % 50 == 0:
                self.logger.info(f"Processed {i + 1}/{len(trades)} trades...")

            historical_trade = self._analyze_trade(trade_data)
            results.all_large_trades.append(historical_trade)
            results.large_trades_analyzed += 1

            if historical_trade.is_fresh_whale:
                results.add_whale(historical_trade)
                self.logger.info(
                    f"WHALE: ${historical_trade.value_usd:,.2f} by "
                    f"{historical_trade.user_address[:10]}... - {historical_trade.detection_reason}"
                )

        self.logger.info(f"Analysis complete. Found {results.fresh_whales_detected} Fresh Whales.")

        return results

    def _analyze_trade(self, trade_data: Dict) -> HistoricalTrade:
        """
        Analyze a single trade for Fresh Whale status.

        Critically, this checks the account state AT THE TIME of the trade.
        """
        user_address = trade_data["user_address"]
        trade_timestamp = trade_data["timestamp"]

        # Get account state at time of trade
        trades_before, first_trade_ts = self.client.get_account_trades_before(
            user_address, trade_timestamp
        )

        # Calculate account age at time of trade
        if first_trade_ts:
            age_seconds = trade_timestamp - first_trade_ts
            age_hours = age_seconds / 3600
        else:
            age_hours = None

        # Determine if Fresh Whale
        is_fresh = False
        reason = ""

        if trades_before < self.config.MAX_HISTORICAL_TRADES:
            is_fresh = True
            reason = f"Only {trades_before} prior trades"
        elif age_hours is not None and age_hours < self.config.NEW_ACCOUNT_HOURS:
            is_fresh = True
            reason = f"Account only {age_hours:.1f} hours old at time of trade"

        return HistoricalTrade(
            id=trade_data["id"],
            user_address=user_address,
            market_id=trade_data["market_id"],
            market_title=trade_data["market_title"],
            outcome=trade_data["outcome"],
            amount=trade_data["amount"],
            price=trade_data["price"],
            value_usd=trade_data["value_usd"],
            timestamp=trade_timestamp,
            tx_hash=trade_data["tx_hash"],
            account_trades_before=trades_before,
            account_first_trade_ts=first_trade_ts,
            account_age_hours_at_trade=age_hours,
            is_fresh_whale=is_fresh,
            detection_reason=reason
        )


# =============================================================================
# EXPORT UTILITIES
# =============================================================================

def export_to_csv(results: BacktestResults, filepath: str):
    """Export backtest results to CSV file."""
    if not results.fresh_whale_trades:
        print(f"No fresh whales to export.")
        return

    fieldnames = list(results.fresh_whale_trades[0].to_dict().keys())

    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for trade in sorted(results.fresh_whale_trades, key=lambda t: t.timestamp, reverse=True):
            writer.writerow(trade.to_dict())

    print(f"Exported {len(results.fresh_whale_trades)} Fresh Whale trades to {filepath}")


def export_all_trades_csv(results: BacktestResults, filepath: str):
    """Export ALL analyzed trades (not just whales) to CSV."""
    if not results.all_large_trades:
        print(f"No trades to export.")
        return

    fieldnames = list(results.all_large_trades[0].to_dict().keys())

    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for trade in sorted(results.all_large_trades, key=lambda t: t.timestamp, reverse=True):
            writer.writerow(trade.to_dict())

    print(f"Exported {len(results.all_large_trades)} trades to {filepath}")


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """Main entry point for backtest CLI."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run historical backtest to validate Fresh Whale detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Backtest last 7 days with default settings
  python backtest.py

  # Backtest last 30 days for trades > $50k
  python backtest.py --days 30 --min-value 50000

  # Export results to CSV
  python backtest.py --days 14 --output whales.csv

  # Export ALL large trades (for analysis)
  python backtest.py --days 7 --output-all all_trades.csv
        """
    )

    parser.add_argument(
        "--days", "-d",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)"
    )
    parser.add_argument(
        "--min-value", "-m",
        type=float,
        default=10000,
        help="Minimum trade value in USD (default: 10000)"
    )
    parser.add_argument(
        "--max-trades", "-t",
        type=int,
        default=5,
        help="Max prior trades for 'new account' (default: 5)"
    )
    parser.add_argument(
        "--account-hours", "-a",
        type=int,
        default=72,
        help="Account age threshold in hours (default: 72)"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        help="Export Fresh Whale results to CSV file"
    )
    parser.add_argument(
        "--output-all",
        type=str,
        help="Export ALL analyzed trades to CSV file"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    # Build configuration
    config = BacktestConfig(
        LOOKBACK_DAYS=args.days,
        MIN_TRADE_VALUE_USD=args.min_value,
        MAX_HISTORICAL_TRADES=args.max_trades,
        NEW_ACCOUNT_HOURS=args.account_hours,
        LOG_LEVEL="DEBUG" if args.debug else "INFO"
    )

    # Run backtest
    engine = BacktestEngine(config)
    results = engine.run()

    # Print summary
    results.print_summary()

    # Export to CSV if requested
    if args.output:
        export_to_csv(results, args.output)

    if args.output_all:
        export_all_trades_csv(results, args.output_all)


if __name__ == "__main__":
    main()
