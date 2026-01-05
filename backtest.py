#!/usr/bin/env python3
"""
Polymarket Whale Monitor - Historical Backtest Module
======================================================

This module provides historical analysis capabilities to validate the
Fresh Whale detection logic by looking back at past trades.

Data Sources:
- Activity Subgraph: Historical trade data (splits)
- Gamma API: Market information
- Data API: User trading history

Usage:
    python backtest.py --days 7 --min-value 10000 --output results.csv
"""

import os
import sys
import time
import json
import logging
import csv
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any, Set, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

import requests

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BacktestConfig:
    """Configuration for historical backtest."""

    # API Endpoints
    ACTIVITY_SUBGRAPH_URL: str = (
        "https://api.goldsky.com/api/public/"
        "project_cl6mb8i9h0003e201j6li0diw/subgraphs/activity-subgraph/0.0.4/gn"
    )
    GAMMA_API_URL: str = "https://gamma-api.polymarket.com"
    DATA_API_URL: str = "https://data-api.polymarket.com"

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
    RATE_LIMIT_DELAY: float = 0.3  # Delay between API calls

    # Logging
    LOG_LEVEL: str = "INFO"


@dataclass
class AggregateBacktestConfig:
    """Configuration for aggregate whale backtest."""

    # API Endpoints (same as BacktestConfig)
    ACTIVITY_SUBGRAPH_URL: str = (
        "https://api.goldsky.com/api/public/"
        "project_cl6mb8i9h0003e201j6li0diw/subgraphs/activity-subgraph/0.0.4/gn"
    )
    GAMMA_API_URL: str = "https://gamma-api.polymarket.com"
    DATA_API_URL: str = "https://data-api.polymarket.com"

    # Aggregate detection thresholds
    AGGREGATE_MIN_POSITION_USD: float = 30_000.0  # Min aggregate position value
    ASYMMETRIC_PRICE_THRESHOLD: float = 0.30  # Max avg price for asymmetric
    MAX_SINGLE_TRADE_FOR_AGGREGATE: float = 10_000.0  # Max single trade (stealth)
    FRESH_ACCOUNT_MAX_TRADES: int = 10  # Max trades for fresh account
    FRESH_ACCOUNT_MAX_HOURS: int = 72  # Max account age in hours

    # Backtest parameters
    LOOKBACK_DAYS: int = 14
    BATCH_SIZE: int = 100
    MAX_ACCOUNTS_TO_ANALYZE: int = 1000

    # API settings
    REQUEST_TIMEOUT: int = 30
    MAX_RETRIES: int = 3
    RETRY_DELAY: int = 5
    RATE_LIMIT_DELAY: float = 0.3

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

    # User profile
    username: str = ""  # Polymarket username if available

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
            "username": self.username,
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


@dataclass
class AggregatePosition:
    """Aggregated position in a single market for backtest analysis."""
    user_address: str
    market_id: str
    market_title: str
    outcome: str
    total_shares: float
    average_price: float
    total_invested_usd: float
    trade_count: int
    first_trade_timestamp: int
    last_trade_timestamp: int
    max_single_trade_usd: float
    # Account state at time of last trade
    account_total_trades: int
    account_first_trade_ts: Optional[int]
    account_age_hours: Optional[float]
    # Detection result
    is_aggregate_whale: bool
    detection_reason: str
    username: str = ""

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
        return self.total_shares

    @property
    def implied_edge(self) -> float:
        if self.total_invested_usd == 0:
            return 0
        return (self.potential_payout - self.total_invested_usd) / self.total_invested_usd

    @property
    def polymarket_profile_url(self) -> str:
        return f"https://polymarket.com/profile/{self.user_address}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "first_trade": self.formatted_first_trade,
            "last_trade": self.formatted_last_trade,
            "total_invested_usd": self.total_invested_usd,
            "potential_payout": self.potential_payout,
            "implied_edge_pct": self.implied_edge * 100,
            "market_title": self.market_title,
            "outcome": self.outcome,
            "average_price": self.average_price,
            "total_shares": self.total_shares,
            "trade_count": self.trade_count,
            "max_single_trade_usd": self.max_single_trade_usd,
            "user_address": self.user_address,
            "username": self.username,
            "account_total_trades": self.account_total_trades,
            "account_age_hours": self.account_age_hours,
            "is_aggregate_whale": self.is_aggregate_whale,
            "detection_reason": self.detection_reason,
            "profile_url": self.polymarket_profile_url
        }


@dataclass
class AggregateBacktestResults:
    """Summary of aggregate whale backtest results."""
    start_time: datetime
    end_time: datetime
    config: AggregateBacktestConfig

    accounts_analyzed: int = 0
    positions_analyzed: int = 0
    aggregate_whales_detected: int = 0

    aggregate_whale_positions: List[AggregatePosition] = field(default_factory=list)
    all_positions: List[AggregatePosition] = field(default_factory=list)

    # Analytics
    unique_whale_addresses: Set[str] = field(default_factory=set)
    total_whale_volume_usd: float = 0.0
    whales_by_market: Dict[str, int] = field(default_factory=dict)

    def add_whale(self, position: AggregatePosition):
        """Add a detected aggregate whale position."""
        self.aggregate_whale_positions.append(position)
        self.aggregate_whales_detected += 1
        self.unique_whale_addresses.add(position.user_address)
        self.total_whale_volume_usd += position.total_invested_usd

        market_key = position.market_title[:50]
        self.whales_by_market[market_key] = self.whales_by_market.get(market_key, 0) + 1

    def print_summary(self):
        """Print a summary of aggregate backtest results."""
        print("\n" + "=" * 70)
        print("AGGREGATE WHALE BACKTEST RESULTS")
        print("=" * 70)
        print(f"Period: {self.start_time.strftime('%Y-%m-%d')} to {self.end_time.strftime('%Y-%m-%d')}")
        print(f"Min aggregate position: ${self.config.AGGREGATE_MIN_POSITION_USD:,.0f}")
        print(f"Asymmetric threshold: <{self.config.ASYMMETRIC_PRICE_THRESHOLD:.0%}")
        print(f"Max single trade: ${self.config.MAX_SINGLE_TRADE_FOR_AGGREGATE:,.0f}")
        print(f"Fresh account: <{self.config.FRESH_ACCOUNT_MAX_TRADES} trades OR <{self.config.FRESH_ACCOUNT_MAX_HOURS}h old")
        print("-" * 70)
        print(f"Accounts analyzed: {self.accounts_analyzed:,}")
        print(f"Positions analyzed: {self.positions_analyzed:,}")
        print(f"Aggregate Whales detected: {self.aggregate_whales_detected:,}")
        print(f"Unique whale addresses: {len(self.unique_whale_addresses):,}")
        print(f"Total whale volume: ${self.total_whale_volume_usd:,.2f}")

        if self.aggregate_whale_positions:
            avg_position = self.total_whale_volume_usd / self.aggregate_whales_detected
            print(f"Average position size: ${avg_position:,.2f}")

            print("\n" + "-" * 70)
            print("TOP 10 LARGEST AGGREGATE WHALE POSITIONS:")
            print("-" * 70)
            sorted_positions = sorted(
                self.aggregate_whale_positions,
                key=lambda p: p.total_invested_usd,
                reverse=True
            )[:10]

            for i, pos in enumerate(sorted_positions, 1):
                print(f"\n{i}. ${pos.total_invested_usd:,.2f} invested -> ${pos.potential_payout:,.2f} potential payout")
                print(f"   Market: {pos.market_title[:55]}...")
                print(f"   Position: {pos.outcome} @ avg {pos.average_price:.1%}")
                print(f"   Built via {pos.trade_count} trades (max single: ${pos.max_single_trade_usd:,.2f})")
                print(f"   Implied edge: {pos.implied_edge * 100:.1f}%")
                print(f"   Reason: {pos.detection_reason}")
                wallet_display = f"{pos.user_address[:12]}...{pos.user_address[-8:]}"
                if pos.username:
                    wallet_display += f" ({pos.username})"
                print(f"   Wallet: {wallet_display}")

        print("\n" + "=" * 70)


# =============================================================================
# API CLIENT
# =============================================================================

class HistoricalClient:
    """Client for fetching historical data from Polymarket APIs."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)

        # Cache for market info and user activity
        self._market_cache: Dict[str, Dict] = {}
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

    def get_historical_splits(
        self,
        start_timestamp: int,
        end_timestamp: int,
        min_amount_raw: int,
        progress_callback=None
    ) -> List[Dict]:
        """
        Fetch historical splits (trades) from Activity Subgraph with pagination.

        Args:
            start_timestamp: Start of time range
            end_timestamp: End of time range
            min_amount_raw: Minimum amount in raw units (6 decimals)
            progress_callback: Optional callback for progress updates

        Returns:
            List of split records
        """
        all_splits = []
        last_id = ""

        # Initial query without ID filter
        query_initial = """
        query GetHistoricalSplits($start: BigInt!, $end: BigInt!, $minAmount: BigInt!, $first: Int!) {
            splits(
                first: $first,
                orderBy: timestamp,
                orderDirection: desc,
                where: {
                    timestamp_gte: $start,
                    timestamp_lte: $end,
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

        # Paginated query with ID filter
        query_paginated = """
        query GetHistoricalSplits($start: BigInt!, $end: BigInt!, $minAmount: BigInt!, $first: Int!, $lastId: String!) {
            splits(
                first: $first,
                orderBy: timestamp,
                orderDirection: desc,
                where: {
                    timestamp_gte: $start,
                    timestamp_lte: $end,
                    amount_gte: $minAmount,
                    id_lt: $lastId
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
            "start": str(start_timestamp),
            "end": str(end_timestamp),
            "minAmount": str(min_amount_raw),
            "first": self.config.BATCH_SIZE
        }

        # First query
        data = self._graphql_query(query_initial, variables)

        if not data or "splits" not in data:
            return []

        splits = data["splits"]
        all_splits.extend(splits)

        if progress_callback:
            progress_callback(len(all_splits))

        # Paginate
        while len(splits) == self.config.BATCH_SIZE:
            if len(all_splits) >= self.config.MAX_TRADES_TO_ANALYZE:
                self.logger.warning(f"Reached max trades limit ({self.config.MAX_TRADES_TO_ANALYZE})")
                break

            last_id = splits[-1]["id"]
            variables["lastId"] = last_id

            time.sleep(self.config.RATE_LIMIT_DELAY)
            data = self._graphql_query(query_paginated, variables)

            if not data or "splits" not in data:
                break

            splits = data["splits"]
            all_splits.extend(splits)

            if progress_callback:
                progress_callback(len(all_splits))

        return all_splits

    def get_market_info(self, condition_id: str) -> Optional[Dict]:
        """Get market information from Gamma API by condition ID."""
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

    def get_user_activity(self, user_address: str) -> List[Dict]:
        """
        Get user's trading activity from Data API.

        Also caches market info from activities since Data API returns
        correct market titles (unlike Gamma API which can be unreliable).
        """
        address = user_address.lower()

        if address in self._user_activity_cache:
            return self._user_activity_cache[address]

        time.sleep(self.config.RATE_LIMIT_DELAY)

        url = f"{self.config.DATA_API_URL}/activity"
        params = {"user": address, "limit": 1000}

        response = self._request_with_retry("GET", url, params=params)

        if response and response.status_code == 200:
            activities = response.json()

            # Cache market info from activities - Data API provides correct titles
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
        """Extract username from user activity data."""
        if activities:
            return activities[0].get("name", "") or ""
        return ""

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
        activities = self.get_user_activity(address)

        if not activities:
            return 0, None

        # Filter for TRADE type activities
        trades = [a for a in activities if a.get("type") == "TRADE"]

        if not trades:
            return 0, None

        # Sort by timestamp ascending
        trades_sorted = sorted(trades, key=lambda x: x.get("timestamp", 0))

        # First trade timestamp
        first_ts = trades_sorted[0].get("timestamp") if trades_sorted else None

        # Count trades before the given timestamp
        trades_before = [t for t in trades_sorted if t.get("timestamp", 0) < before_timestamp]

        return len(trades_before), first_ts

    def aggregate_trades_to_positions(
        self,
        address: str,
        lookback_days: int = 14
    ) -> List[Dict]:
        """
        Aggregate an account's trades by market/outcome to find positions.

        Returns a list of position dicts with aggregated metrics.
        """
        activities = self.get_user_activity(address)

        if not activities:
            return []

        # Filter for TRADE type activities within lookback period
        lookback_ts = int(time.time()) - (lookback_days * 86400)
        trades = [
            a for a in activities
            if a.get("type") == "TRADE" and a.get("timestamp", 0) >= lookback_ts
        ]

        if not trades:
            return []

        # Aggregate trades by (market_id, outcome)
        position_map: Dict[tuple, List[Dict]] = {}

        for trade in trades:
            cond_id = trade.get("conditionId", "")
            outcome = trade.get("outcome", "Unknown")
            key = (cond_id, outcome)

            if key not in position_map:
                position_map[key] = []
            position_map[key].append(trade)

        # Build position summaries
        positions = []
        for (cond_id, outcome), trade_list in position_map.items():
            total_shares = 0.0
            total_invested = 0.0
            max_single_trade = 0.0
            first_ts = float('inf')
            last_ts = 0
            market_title = "Unknown Market"

            for trade in trade_list:
                shares = trade.get("outcomeTokensAmount", 0)
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

                if market_title == "Unknown Market":
                    title = trade.get("title", "")
                    if title:
                        market_title = title
                    elif cond_id in self._market_cache:
                        market_title = self._market_cache[cond_id].get(
                            "question", "Unknown Market"
                        )

            avg_price = total_invested / total_shares if total_shares > 0 else 0
            if first_ts == float('inf'):
                first_ts = 0

            positions.append({
                "market_id": cond_id,
                "market_title": market_title,
                "outcome": outcome,
                "total_shares": total_shares,
                "average_price": avg_price,
                "total_invested_usd": total_invested,
                "trade_count": len(trade_list),
                "first_trade_timestamp": int(first_ts),
                "last_trade_timestamp": int(last_ts),
                "max_single_trade_usd": max_single_trade
            })

        return positions


# =============================================================================
# BACKTEST ENGINE
# =============================================================================

class BacktestEngine:
    """Engine for running historical backtest analysis."""

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()
        self.client = HistoricalClient(self.config)
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

        # Convert USD to raw amount (6 decimals for USDC)
        min_amount_raw = int(self.config.MIN_TRADE_VALUE_USD * 1_000_000)

        # Fetch historical splits
        self.logger.info("Fetching historical trades...")

        def progress_callback(count):
            self.logger.info(f"  Fetched {count} splits...")

        splits = self.client.get_historical_splits(
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            min_amount_raw=min_amount_raw,
            progress_callback=progress_callback
        )

        results.total_trades_fetched = len(splits)
        self.logger.info(f"Found {len(splits)} large splits")

        # Analyze each split
        self.logger.info("Analyzing trades for Fresh Whale patterns...")

        for i, split in enumerate(splits):
            if (i + 1) % 25 == 0:
                self.logger.info(f"  Processed {i + 1}/{len(splits)} trades...")

            historical_trade = self._analyze_split(split)
            if historical_trade:
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

    def _analyze_split(self, split: Dict) -> Optional[HistoricalTrade]:
        """
        Analyze a single split for Fresh Whale status.

        Critically, this checks the account state AT THE TIME of the trade.
        """
        try:
            split_id = split["id"]
            timestamp = int(split["timestamp"])
            stakeholder = split["stakeholder"]
            condition = split["condition"]
            amount_raw = int(split["amount"])
            value_usd = amount_raw / 1_000_000

            # Get user activity first - this populates market cache with correct titles
            # and allows us to extract username
            activities = self.client.get_user_activity(stakeholder)
            username = self.client.get_username_from_activity(activities)

            # Try to get market info from cache (populated by user activity with correct titles)
            market_info = self.client._market_cache.get(condition)

            if market_info:
                market_title = market_info.get("question", "Unknown Market")
                outcome = market_info.get("outcome", "Position")
                market_id = condition
            else:
                # Fallback: try Gamma API (may not be reliable for all markets)
                gamma_info = self.client.get_market_info(condition)
                if gamma_info:
                    market_title = gamma_info.get("question", "Unknown Market")
                    market_id = gamma_info.get("id", condition)
                else:
                    market_title = f"Market {condition[:16]}..."
                    market_id = condition
                outcome = "Position"

            # Get account state at time of trade
            trades_before, first_trade_ts = self.client.get_account_trades_before(
                stakeholder, timestamp
            )

            # Calculate account age at time of trade
            if first_trade_ts:
                age_seconds = timestamp - first_trade_ts
                age_hours = max(0, age_seconds / 3600)
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
                id=split_id,
                user_address=stakeholder,
                market_id=market_id,
                market_title=market_title,
                outcome=outcome,
                amount=value_usd,
                price=1.0,
                value_usd=value_usd,
                timestamp=timestamp,
                tx_hash=split_id.split("_")[0] if "_" in split_id else split_id,
                account_trades_before=trades_before,
                account_first_trade_ts=first_trade_ts,
                account_age_hours_at_trade=age_hours,
                is_fresh_whale=is_fresh,
                detection_reason=reason,
                username=username
            )

        except (KeyError, ValueError, TypeError) as e:
            self.logger.warning(f"Failed to analyze split: {e}")
            return None


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


def export_aggregate_csv(results: AggregateBacktestResults, filepath: str):
    """Export aggregate whale results to CSV file."""
    if not results.aggregate_whale_positions:
        print("No aggregate whales to export.")
        return

    fieldnames = list(results.aggregate_whale_positions[0].to_dict().keys())

    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for pos in sorted(results.aggregate_whale_positions,
                         key=lambda p: p.total_invested_usd, reverse=True):
            writer.writerow(pos.to_dict())

    print(f"Exported {len(results.aggregate_whale_positions)} aggregate whale positions to {filepath}")


# =============================================================================
# AGGREGATE BACKTEST ENGINE
# =============================================================================

def run_aggregate_backtest(
    config: Optional[AggregateBacktestConfig] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> AggregateBacktestResults:
    """
    Run historical backtest for Aggregate Asymmetric Whale detection.

    This function:
    1. Finds accounts with significant trading activity
    2. Aggregates their trades by market
    3. Checks against AAW detection criteria
    4. Returns results with all qualifying positions
    """
    config = config or AggregateBacktestConfig()

    # Setup logging
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger = logging.getLogger("AggregateBacktest")

    # Set time range
    if end_date is None:
        end_date = datetime.now(timezone.utc)
    if start_date is None:
        start_date = end_date - timedelta(days=config.LOOKBACK_DAYS)

    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())

    results = AggregateBacktestResults(
        start_time=start_date,
        end_time=end_date,
        config=config
    )

    logger.info("=" * 60)
    logger.info("Starting Aggregate Whale Backtest")
    logger.info(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    logger.info(f"Min aggregate position: ${config.AGGREGATE_MIN_POSITION_USD:,.0f}")
    logger.info(f"Asymmetric threshold: <{config.ASYMMETRIC_PRICE_THRESHOLD:.0%}")
    logger.info("=" * 60)

    # Create a client (reusing BacktestConfig-compatible settings)
    class AggregateClient(HistoricalClient):
        def __init__(self, agg_config: AggregateBacktestConfig):
            # Create a BacktestConfig with matching settings
            base_config = BacktestConfig(
                ACTIVITY_SUBGRAPH_URL=agg_config.ACTIVITY_SUBGRAPH_URL,
                GAMMA_API_URL=agg_config.GAMMA_API_URL,
                DATA_API_URL=agg_config.DATA_API_URL,
                LOOKBACK_DAYS=agg_config.LOOKBACK_DAYS,
                BATCH_SIZE=agg_config.BATCH_SIZE,
                REQUEST_TIMEOUT=agg_config.REQUEST_TIMEOUT,
                MAX_RETRIES=agg_config.MAX_RETRIES,
                RETRY_DELAY=agg_config.RETRY_DELAY,
                RATE_LIMIT_DELAY=agg_config.RATE_LIMIT_DELAY
            )
            super().__init__(base_config)
            self.agg_config = agg_config

    client = AggregateClient(config)

    # Step 1: Find accounts with significant activity
    logger.info("Finding accounts with significant activity...")

    # Use splits to find active accounts
    min_amount_raw = int(1000 * 1_000_000)  # $1000 minimum per trade

    query = """
    query GetRecentSplits($start: BigInt!, $end: BigInt!, $minAmount: BigInt!, $first: Int!) {
        splits(
            first: $first,
            orderBy: timestamp,
            orderDirection: desc,
            where: {
                timestamp_gte: $start,
                timestamp_lte: $end,
                amount_gte: $minAmount
            }
        ) {
            stakeholder
            amount
        }
    }
    """

    variables = {
        "start": str(start_ts),
        "end": str(end_ts),
        "minAmount": str(min_amount_raw),
        "first": 1000
    }

    data = client._graphql_query(query, variables)

    active_accounts: Dict[str, float] = {}
    if data and "splits" in data:
        for split in data["splits"]:
            addr = split["stakeholder"]
            amount = int(split["amount"]) / 1_000_000
            if addr not in active_accounts:
                active_accounts[addr] = 0
            active_accounts[addr] += amount

    # Filter to accounts with sufficient volume
    candidate_accounts = [
        addr for addr, vol in active_accounts.items()
        if vol >= config.AGGREGATE_MIN_POSITION_USD
    ]

    # Limit to max accounts
    candidate_accounts = candidate_accounts[:config.MAX_ACCOUNTS_TO_ANALYZE]

    logger.info(f"Found {len(candidate_accounts)} candidate accounts to analyze")
    results.accounts_analyzed = len(candidate_accounts)

    # Step 2: Analyze each account's positions
    for i, address in enumerate(candidate_accounts):
        if (i + 1) % 25 == 0:
            logger.info(f"  Processed {i + 1}/{len(candidate_accounts)} accounts...")

        try:
            # Get user activity and profile info
            activities = client.get_user_activity(address)
            username = client.get_username_from_activity(activities)

            # Get account stats
            trades = [a for a in activities if a.get("type") == "TRADE"]
            if not trades:
                continue

            trades_sorted = sorted(trades, key=lambda x: x.get("timestamp", 0))
            first_trade_ts = trades_sorted[0].get("timestamp") if trades_sorted else None
            total_trades = len(trades)

            # Calculate account age
            if first_trade_ts:
                age_hours = (time.time() - first_trade_ts) / 3600
            else:
                age_hours = None

            # Check fresh account criteria
            is_fresh = (
                total_trades < config.FRESH_ACCOUNT_MAX_TRADES or
                (age_hours is not None and age_hours < config.FRESH_ACCOUNT_MAX_HOURS)
            )

            if not is_fresh:
                continue

            # Get aggregated positions
            positions = client.aggregate_trades_to_positions(address, config.LOOKBACK_DAYS)

            for pos_data in positions:
                results.positions_analyzed += 1

                # Check all aggregate whale criteria
                # 1. Aggregate position value >= threshold
                if pos_data["total_invested_usd"] < config.AGGREGATE_MIN_POSITION_USD:
                    continue

                # 2. Asymmetric (low-odds position)
                if pos_data["average_price"] > config.ASYMMETRIC_PRICE_THRESHOLD:
                    continue

                # 3. Stealth accumulation (no single trade exceeds threshold)
                if pos_data["max_single_trade_usd"] >= config.MAX_SINGLE_TRADE_FOR_AGGREGATE:
                    continue

                # 4. Multiple trades
                if pos_data["trade_count"] < 2:
                    continue

                # All criteria met - this is an Aggregate Whale!
                reasons = []
                if total_trades < config.FRESH_ACCOUNT_MAX_TRADES:
                    reasons.append(f"only {total_trades} total trades")
                if age_hours is not None and age_hours < config.FRESH_ACCOUNT_MAX_HOURS:
                    reasons.append(f"account {age_hours:.1f}h old")

                reason = (
                    f"Built ${pos_data['total_invested_usd']:,.0f} via "
                    f"{pos_data['trade_count']} trades at avg {pos_data['average_price']:.1%} "
                    f"({', '.join(reasons)})"
                )

                position = AggregatePosition(
                    user_address=address,
                    market_id=pos_data["market_id"],
                    market_title=pos_data["market_title"],
                    outcome=pos_data["outcome"],
                    total_shares=pos_data["total_shares"],
                    average_price=pos_data["average_price"],
                    total_invested_usd=pos_data["total_invested_usd"],
                    trade_count=pos_data["trade_count"],
                    first_trade_timestamp=pos_data["first_trade_timestamp"],
                    last_trade_timestamp=pos_data["last_trade_timestamp"],
                    max_single_trade_usd=pos_data["max_single_trade_usd"],
                    account_total_trades=total_trades,
                    account_first_trade_ts=first_trade_ts,
                    account_age_hours=age_hours,
                    is_aggregate_whale=True,
                    detection_reason=reason,
                    username=username
                )

                results.add_whale(position)
                logger.info(
                    f"AGGREGATE WHALE: ${position.total_invested_usd:,.2f} by "
                    f"{address[:10]}... in {position.market_title[:40]}..."
                )

            # Rate limit
            time.sleep(config.RATE_LIMIT_DELAY)

        except Exception as e:
            logger.warning(f"Error analyzing account {address[:10]}...: {e}")
            continue

    logger.info(f"Analysis complete. Found {results.aggregate_whales_detected} aggregate whales.")
    results.print_summary()

    return results


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
    # Aggregate detection arguments
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Run aggregate whale detection instead of single-trade detection"
    )
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
        "--output-aggregate",
        type=str,
        help="Export aggregate whale results to CSV file"
    )

    args = parser.parse_args()

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

    # Build configuration for standard fresh whale backtest
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
