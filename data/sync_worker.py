import asyncio
from datetime import datetime

import sqlalchemy
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models import Balance, Position
from soad.utils.logger import logger
from soad.utils.utils import (
    extract_option_details,
    futures_contract_size,
    is_futures_symbol,
    is_option,
)

UPDATE_UNCATEGORIZED_POSITIONS = False
# TODO: harden/fix this (super buggy right now)
RECONCILE_POSITIONS = False
TIMEOUT_DURATION = 120


class BrokerService:
    def __init__(self, brokers):
        self.brokers = brokers

    async def get_broker_instance(self, broker_name):
        logger.debug(f"Getting broker instance for {broker_name}")
        return await self._fetch_broker_instance(broker_name)

    async def _fetch_broker_instance(self, broker_name):
        return self.brokers[broker_name]

    async def get_latest_price(self, broker_name, symbol):
        broker_instance = await self.get_broker_instance(broker_name)
        return await self._fetch_price(broker_instance, symbol)

    async def get_account_info(self, broker_name):
        broker_instance = await self.get_broker_instance(broker_name)
        return await broker_instance.get_account_info()

    async def _fetch_price(self, broker_instance, symbol):
        if asyncio.iscoroutinefunction(broker_instance.get_current_price):
            return await broker_instance.get_current_price(symbol)
        return broker_instance.get_current_price(symbol)


class PositionService:
    def __init__(self, broker_service):
        self.broker_service = broker_service

    async def reconcile_positions(self, session, broker, timestamp=None):
        now = timestamp or datetime.now()
        broker_positions, db_positions = await self._get_positions(session, broker)
        await self._remove_excess_uncategorized_positions(
            session, broker, db_positions, broker_positions
        )
        await self._remove_db_positions(session, broker, db_positions, broker_positions)
        broker_positions, db_positions = await self._get_positions(session, broker)
        await self._add_missing_positions(
            session, broker, db_positions, broker_positions, now
        )
        session.add_all(db_positions.values())  # Add updated positions to the session
        await session.commit()
        logger.info(f"Reconciliation for broker {broker} completed.")

    async def _remove_excess_uncategorized_positions(
        self, session, broker, db_positions, broker_positions
    ):
        """
        Removes excess uncategorized positions if more shares or contracts exist in DB than in the broker.
        Subtracts the quantity of categorized positions for the same symbol from the broker quantity.
        """
        for symbol, db_position in db_positions.items():
            if self._is_uncategorized_position(db_position):
                broker_position = broker_positions.get(symbol)
                if not broker_position:
                    # Delete uncategorized positions that no longer exist in the broker
                    await session.delete(db_position)
                    logger.info(f"Removed uncategorized position from DB: {symbol}")
                    continue
                categorized_quantity = self._get_categorized_quantity(
                    db_positions, symbol
                )
                net_broker_quantity = self._calculate_net_broker_quantity(
                    broker_position["quantity"], categorized_quantity
                )
                await self._adjust_uncategorized_position(
                    session, db_position, net_broker_quantity, symbol
                )

    def _is_uncategorized_position(self, db_position):
        """
        Checks if the position is uncategorized and exists in broker positions.
        """
        return db_position.strategy == "uncategorized"

    def _get_categorized_quantity(self, db_positions, symbol):
        """
        Calculates the total quantity for categorized positions for the given symbol.
        """
        return sum(
            pos.quantity
            for pos in db_positions.values()
            if pos.symbol == symbol and pos.strategy != "uncategorized"
        )

    def _calculate_net_broker_quantity(self, broker_quantity, categorized_quantity):
        """
        Subtracts categorized quantity from broker quantity to get the net broker quantity.
        """
        return max(broker_quantity - categorized_quantity, 0)

    async def _adjust_uncategorized_position(
        self, session, db_position, net_broker_quantity, symbol
    ):
        """
        Adjusts the uncategorized position's quantity if it exceeds the net broker quantity.
        """
        if db_position.quantity > net_broker_quantity:
            excess_quantity = max(db_position.quantity - net_broker_quantity, 0)
            logger.info(
                f"Removing excess quantity {excess_quantity} for {symbol} in uncategorized positions."
            )
            db_position.quantity = net_broker_quantity
            session.add(db_position)

    async def _get_positions(self, session, broker):
        broker_instance = await self.broker_service.get_broker_instance(broker)
        broker_positions = broker_instance.get_positions()
        db_positions = await self._fetch_db_positions(session, broker)
        return broker_positions, db_positions

    async def _fetch_db_positions(self, session, broker):
        db_positions_result = await session.execute(
            select(Position).filter_by(broker=broker)
        )
        return {pos.symbol: pos for pos in db_positions_result.scalars().all()}

    async def _remove_db_positions(
        self, session, broker, db_positions, broker_positions
    ):
        broker_symbols = set(broker_positions.keys())
        db_symbols = set(db_positions.keys())
        symbols_to_remove = db_symbols - broker_symbols
        if symbols_to_remove:
            await session.execute(
                sqlalchemy.delete(Position).where(
                    Position.broker == broker, Position.symbol.in_(symbols_to_remove)
                )
            )
            logger.info(
                f"Removed positions from DB for broker {broker}: {symbols_to_remove}"
            )

    async def _add_missing_positions(
        self, session, broker, db_positions, broker_positions, now
    ):
        for symbol, broker_position in broker_positions.items():
            if symbol in db_positions:
                existing_position = db_positions[symbol]
                self._update_existing_position(existing_position, broker_position, now)
            else:
                logger.warn(f"Found uncategorized position in broker: {symbol}")
                if UPDATE_UNCATEGORIZED_POSITIONS:
                    await self._insert_new_position(
                        session, broker, broker_position, now
                    )

    def _update_existing_position(self, existing_position, broker_position, now):
        logger.info(
            "Updating existing position in DB: {existing_position.symbol}",
            extra={
                "symbol": existing_position.symbol,
                "quantity": broker_position["quantity"],
                "old_quantity": existing_position.quantity,
            },
        )
        existing_position.quantity = broker_position["quantity"]
        existing_position.last_updated = now

    async def _insert_new_position(self, session, broker, broker_position, now):
        price = await self.broker_service.get_latest_price(
            broker, broker_position["symbol"]
        )
        new_position = Position(
            broker=broker,
            strategy="uncategorized",
            symbol=broker_position["symbol"],
            latest_price=price,
            quantity=broker_position["quantity"],
            last_updated=now,
        )
        session.add(new_position)
        logger.info(f"Added uncategorized position to DB: {new_position.symbol}")

    async def update_position_cost_basis(self, session, position, broker_instance):
        """
        Fetch and update the cost basis for a position.
        """
        logger.debug(f"Fetching cost basis for {position.symbol}")
        try:
            cost_basis = broker_instance.get_cost_basis(position.symbol)
            if cost_basis is not None:
                position.cost_basis = cost_basis
                logger.info(f"Updated cost basis for {position.symbol}: {cost_basis}")
                session.add(position)
            else:
                logger.error(f"Failed to retrieve cost basis for {position.symbol}")
        except Exception as e:
            logger.error(f"Error updating cost basis for {position.symbol}: {e}")

    async def update_position_prices_and_volatility(
        self, session, positions, timestamp
    ):
        now_naive = self._strip_timezone(timestamp or datetime.now())
        await self._update_prices_and_volatility(session, positions, now_naive)
        await session.commit()
        logger.info("Completed updating latest prices and volatility")

    def _strip_timezone(self, timestamp):
        return timestamp.replace(tzinfo=None)

    # TODO: fix or remove
    async def update_cost_basis(self, session, position):
        broker_instance = await self.broker_service.get_broker_instance(position.broker)
        await self.update_position_cost_basis(session, position, broker_instance)

    async def _update_prices_and_volatility(self, session, positions, now_naive):
        for position in positions:
            try:
                await self._update_position_price(session, position, now_naive)
                if RECONCILE_POSITIONS:
                    await self.update_cost_basis(session, position)
            except Exception:
                logger.exception(f"Error processing position {position.symbol}")

    async def _update_position_price(self, session, position, now_naive):
        latest_price = await self._fetch_and_log_price(position)
        if not latest_price:
            return

        position.latest_price, position.last_updated = latest_price, now_naive
        underlying_symbol = self._get_underlying_symbol(position)
        await self._update_volatility_and_underlying_price(
            session, position, underlying_symbol
        )

    async def _fetch_and_log_price(self, position):
        latest_price = await self.broker_service.get_latest_price(
            position.broker, position.symbol
        )
        if latest_price is None:
            logger.error(f"Could not get latest price for {position.symbol}")
        else:
            logger.debug(
                f"Updated latest price for {position.symbol} to {latest_price}"
            )
        return latest_price

    async def _update_volatility_and_underlying_price(
        self, session, position, underlying_symbol
    ):
        latest_underlying_price = await self.broker_service.get_latest_price(
            position.broker, underlying_symbol
        )
        volatility = await self._calculate_historical_volatility(underlying_symbol)

        if volatility is not None:
            position.underlying_volatility = float(volatility)
            position.underlying_latest_price = float(latest_underlying_price)
            logger.debug(f"Updated volatility for {position.symbol} to {volatility}")
        else:
            logger.error(f"Could not calculate volatility for {underlying_symbol}")
        session.add(position)

    @staticmethod
    def _get_underlying_symbol(position):
        return (
            extract_option_details(position.symbol)[0]
            if is_option(position.symbol)
            else position.symbol
        )

    @staticmethod
    async def _calculate_historical_volatility(symbol):
        logger.debug(f"Calculating historical volatility for {symbol}")
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period="1y")
            hist["returns"] = hist["Close"].pct_change()
            return hist["returns"].std() * (252**0.5)
        except Exception as e:
            logger.error(f"Error calculating volatility for {symbol}: {e}")
            return None


class BalanceService:
    def __init__(self, broker_service):
        self.broker_service = broker_service

    async def update_all_strategy_balances(self, session, broker, timestamp):
        strategies = await self._get_strategies(session, broker)
        await self._update_each_strategy_balance(session, broker, strategies, timestamp)
        await self.update_uncategorized_balances(session, broker, timestamp)
        await session.commit()
        logger.info(f"Updated all strategy balances for broker {broker}")

    async def _get_strategies(self, session, broker):
        strategies_result = await session.execute(
            select(Balance.strategy)
            .filter_by(broker=broker)
            .distinct()
            .where(Balance.strategy != "uncategorized")
        )
        return strategies_result.scalars().all()

    async def _update_each_strategy_balance(
        self, session, broker, strategies, timestamp
    ):
        for strategy in strategies:
            await self.update_strategy_balance(session, broker, strategy, timestamp)

    async def update_strategy_balance(self, session, broker, strategy, timestamp):
        cash_balance = await self._get_cash_balance(session, broker, strategy)
        positions_balance = await self._calculate_positions_balance(
            session, broker, strategy
        )

        self._insert_or_update_balance(
            session, broker, strategy, "cash", cash_balance, timestamp
        )
        self._insert_or_update_balance(
            session, broker, strategy, "positions", positions_balance, timestamp
        )

        total_balance = cash_balance + positions_balance
        self._insert_or_update_balance(
            session, broker, strategy, "total", total_balance, timestamp
        )

    async def _get_cash_balance(self, session, broker, strategy):
        balance_result = await session.execute(
            select(Balance)
            .filter_by(broker=broker, strategy=strategy, type="cash")
            .order_by(Balance.timestamp.desc())
            .limit(1)
        )
        balance = balance_result.scalar()
        return balance.balance if balance else 0

    async def _calculate_positions_balance(self, session, broker, strategy):
        positions_result = await session.execute(
            select(Position).filter_by(broker=broker, strategy=strategy)
        )
        positions = positions_result.scalars().all()

        total_positions_value = 0
        for position in positions:
            latest_price = await self.broker_service.get_latest_price(
                broker, position.symbol
            )
            if is_option(position.symbol):
                latest_price = latest_price * position.quantity * 100
            elif is_futures_symbol(position.symbol):
                latest_price = (
                    latest_price
                    * position.quantity
                    * futures_contract_size(position.symbol)
                )
            position_value = latest_price * position.quantity
            total_positions_value += position_value

        return total_positions_value

    def _insert_or_update_balance(
        self, session, broker, strategy, balance_type, balance_value, timestamp=None
    ):
        timestamp = timestamp or datetime.now()
        new_balance_record = Balance(
            broker=broker,
            strategy=strategy,
            type=balance_type,
            balance=balance_value,
            timestamp=timestamp,
        )
        session.add(new_balance_record)
        logger.debug(
            f"Updated {balance_type} balance for strategy {strategy}: {balance_value}"
        )

    async def update_uncategorized_balances(self, session, broker, timestamp):
        total_value, categorized_balance_sum = await self._get_account_balance_info(
            session, broker
        )
        logger.info(
            f"Broker {broker}: Total account value: {total_value}, Categorized balance sum: {categorized_balance_sum}"
        )

        uncategorized_balance = max(0, total_value - categorized_balance_sum)
        logger.debug(
            f"Calculated uncategorized balance for broker {broker}: {uncategorized_balance}"
        )

        self._insert_uncategorized_balance(
            session, broker, uncategorized_balance, timestamp
        )

    async def _get_account_balance_info(self, session, broker):
        account_info = await self.broker_service.get_account_info(broker)
        total_value = account_info["value"]
        categorized_balance_sum = await self._sum_all_strategy_balances(session, broker)
        return total_value, categorized_balance_sum

    def _insert_uncategorized_balance(
        self, session, broker, uncategorized_balance, timestamp
    ):
        new_balance_record = Balance(
            broker=broker,
            strategy="uncategorized",
            type="cash",
            balance=uncategorized_balance,
            timestamp=timestamp,
        )
        session.add(new_balance_record)
        logger.debug(
            f"Updated uncategorized balance for broker {broker}: {uncategorized_balance}"
        )

    async def _sum_all_strategy_balances(self, session, broker):
        strategies = await self._get_strategies(session, broker)
        return await self._sum_each_strategy_balance(session, broker, strategies)

    async def _sum_each_strategy_balance(self, session, broker, strategies):
        total_balance = 0
        for strategy in strategies:
            cash_balance = await self._get_cash_balance(session, broker, strategy)
            positions_balance = await self._calculate_positions_balance(
                session, broker, strategy
            )
            logger.info(
                f"Strategy: {strategy}, Cash: {cash_balance}, Positions: {positions_balance}"
            )
            total_balance += cash_balance + positions_balance
        return total_balance


async def start(engine, brokers, timeout_duration=TIMEOUT_DURATION):
    async_engine = await _get_async_engine(engine)
    Session = sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=True
    )

    broker_service = BrokerService(brokers)
    position_service = PositionService(broker_service)
    balance_service = BalanceService(broker_service)

    await _run_sync_worker_iteration(
        Session,
        position_service,
        balance_service,
        brokers,
        timeout_duration=timeout_duration,
    )


async def _get_async_engine(engine):
    if isinstance(engine, str):
        return create_async_engine(engine)
    if isinstance(engine, sqlalchemy.engine.Engine):
        raise ValueError("AsyncEngine expected, but got a synchronous Engine.")
    if isinstance(engine, sqlalchemy.ext.asyncio.AsyncEngine):
        return engine
    raise ValueError(
        "Invalid engine type. Expected a connection string or an AsyncEngine object."
    )


async def _run_sync_worker_iteration(
    Session, position_service, balance_service, brokers, timeout_duration
):
    try:
        await asyncio.wait_for(
            _run_sync_worker_iteration_logic(
                Session, position_service, balance_service, brokers
            ),
            timeout=timeout_duration,
        )
    except asyncio.TimeoutError:
        logger.error("Iteration exceeded the maximum allowed time. Forcing restart.")
        raise


async def _run_sync_worker_iteration_logic(
    Session, position_service, balance_service, brokers
):
    logger.info("Starting sync worker iteration")
    now = datetime.now()
    async with Session() as session:
        logger.info("Session started")
        try:
            await _reconcile_brokers_and_update_balances(
                session, position_service, balance_service, brokers, now
            )
        except Exception as e:
            logger.exception(f"Error reconciling brokers and updating balances: {e}")
        try:
            await _fetch_and_update_positions(session, position_service, now)
        except Exception as e:
            logger.exception(f"Error fetching and updating positions: {e}")
        # commit anything we forgot about
        await session.commit()
    logger.info("Sync worker completed an iteration")


async def _fetch_and_update_positions(session, position_service, now):
    positions = await session.execute(select(Position))
    logger.info("Positions fetched")
    await position_service.update_position_prices_and_volatility(
        session, positions.scalars(), now
    )


async def _reconcile_brokers_and_update_balances(
    session, position_service, balance_service, brokers, now
):
    for broker in brokers:
        if RECONCILE_POSITIONS:
            await position_service.reconcile_positions(session, broker)
        await balance_service.update_all_strategy_balances(session, broker, now)
