#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
# Copyright (C) 2023, 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#
"""
This module controls the reliability for application service transactions.

The nominal flow through this module looks like:
              __________
1---ASa[e]-->|  Service |--> Queue ASa[f]
2----ASb[e]->|  Queuer  |
3--ASa[f]--->|__________|-----------+ ASa[e], ASb[e]
                                    V
      -````````-            +------------+
      |````````|<--StoreTxn-|Transaction |
      |Database|            | Controller |---> SEND TO AS
      `--------`            +------------+
What happens on SEND TO AS depends on the state of the Application Service:
 - If the AS is marked as DOWN, do nothing.
 - If the AS is marked as UP, send the transaction.
     * SUCCESS : Increment where the AS is up to txn-wise and nuke the txn
                 contents from the db.
     * FAILURE : Marked AS as DOWN and start Recoverer.

Recoverer attempts to recover ASes who have died. The flow for this looks like:
                ,--------------------- backoff++ --------------.
               V                                               |
  START ---> Wait exp ------> Get oldest txn ID from ----> FAILURE
             backoff           DB and try to send it
                                 ^                |___________
Mark AS as                       |                            V
UP & quit           +---------- YES                       SUCCESS
    |               |                                         |
    NO <--- Have more txns? <------ Mark txn success & nuke <-+
                                      from db; incr AS pos.
                                         Reset backoff.

This is all tied together by the AppServiceScheduler which DIs the required
components.
"""

import logging
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Sequence,
)

from synapse.appservice import (
    ApplicationService,
    ApplicationServiceState,
    TransactionOneTimeKeysCount,
    TransactionUnusedFallbackKeys,
)
from synapse.appservice.api import ApplicationServiceApi
from synapse.events import EventBase
from synapse.logging.context import run_in_background
from synapse.storage.databases.main import DataStore
from synapse.types import DeviceListUpdates, JsonMapping
from synapse.util.clock import Clock, DelayedCallWrapper
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# Maximum number of events to provide in an AS transaction.
MAX_PERSISTENT_EVENTS_PER_TRANSACTION = 100

# Maximum number of ephemeral events to provide in an AS transaction.
MAX_EPHEMERAL_EVENTS_PER_TRANSACTION = 100

# Maximum number of to-device messages to provide in an AS transaction.
MAX_TO_DEVICE_MESSAGES_PER_TRANSACTION = 100

# IXO-PATCH(as-self-heal): how often the process that sends appservice transactions
# looks in the database for appservices marked DOWN that it has no recoverer for.
RECOVER_DOWN_SERVICES_INTERVAL = Duration(seconds=60)


class ApplicationServiceScheduler:
    """
    Public facing API for this module. Does the required dependency injection (DI) to
    tie the components together. This also serves as the "event_pool", which in this
    case is a simple array.
    """

    def __init__(self, hs: "HomeServer"):
        self.txn_ctrl = _TransactionController(hs)
        self.store = hs.get_datastores().main
        self.queuer = _ServiceQueuer(self.txn_ctrl, hs)

        # IXO-PATCH(as-self-heal): the DOWN state of an appservice lives in the
        # database, but recoverers live in the process that sends transactions, and
        # `start()` only looks for DOWN services once. A DOWN written after that by
        # another process (for example the old pod during a rolling restart) would
        # otherwise never be retried. Only the process that sends appservice
        # transactions runs this check.
        if hs.config.worker.should_notify_appservices:
            hs.get_clock().looping_call(
                hs.run_as_background_process,
                RECOVER_DOWN_SERVICES_INTERVAL,
                desc="as_recover_down_services",
                func=self.start_recoverers_for_down_services,
            )

    async def start(self) -> None:
        logger.info("Starting appservice scheduler")

        # check for any DOWN ASes and start recoverers for them.
        await self.start_recoverers_for_down_services()

    async def start_recoverers_for_down_services(self) -> None:
        """IXO-PATCH(as-self-heal): start a recoverer for every appservice that the
        database marks DOWN and that has no recoverer in this process yet.
        """
        if not self.store.get_app_services():
            return

        services = await self.store.get_appservices_by_state(
            ApplicationServiceState.DOWN
        )

        for service in services:
            if self.txn_ctrl.is_recovering(service):
                continue
            logger.warning(
                "AS ID %s is marked down but this process has no recoverer for it",
                service.id,
            )
            self.txn_ctrl.start_recoverer(service)

    def is_recovering(self, service: ApplicationService) -> bool:
        """IXO-PATCH(as-self-heal): whether this process is retrying transactions
        for the given service, i.e. it cannot take new transactions right now.
        """
        return self.txn_ctrl.is_recovering(service)

    def get_to_device_queued_position(
        self, service: ApplicationService
    ) -> tuple[int | None, int]:
        """IXO-PATCH(as-to-device): where the next read of to-device messages for
        this service should start.

        Returns:
            A tuple of:
              * the to-device stream position up to which messages have been read
                and queued in this process, or None if the read should start from the
                position stored in the database (which only moves after delivery);
              * the generation to pass to `enqueue_to_device_messages`.
        """
        return (
            self.queuer.to_device_queued_pos.get(service.id),
            self.queuer.to_device_generation.get(service.id, 0),
        )

    def enqueue_to_device_messages(
        self,
        appservice: ApplicationService,
        to_device_messages: Collection[JsonMapping],
        stream_pos: int,
        generation: int,
    ) -> None:
        """IXO-PATCH(as-to-device): queue the to-device messages that were read for
        this service up to `stream_pos` (inclusive).

        `stream_pos` is stored in the database only once the transaction carrying the
        last of these messages has been delivered. If undelivered to-device messages
        were dropped since `get_to_device_queued_position` returned `generation`, the
        read is discarded: the messages are read again from the stored position.

        Args:
            appservice: The application service the messages are for.
            to_device_messages: The messages, in stream order. May be empty.
            stream_pos: The to-device stream position the read covered.
            generation: As returned by `get_to_device_queued_position` before the read.
        """
        queuer = self.queuer
        if queuer.to_device_generation.get(appservice.id, 0) != generation:
            logger.info(
                "Discarding a to-device read for AS ID %s: undelivered messages were "
                "dropped while it ran, they will be read again",
                appservice.id,
            )
            return

        if to_device_messages:
            queuer.queued_to_device_messages.setdefault(appservice.id, []).extend(
                to_device_messages
            )
        queuer.to_device_queued_pos[appservice.id] = stream_pos

        # Also kicked off when there are no messages, so that the sender stores the
        # new position once nothing for this service is outstanding.
        queuer.start_background_request(appservice)

    def enqueue_for_appservice(
        self,
        appservice: ApplicationService,
        events: Collection[EventBase] | None = None,
        ephemeral: Collection[JsonMapping] | None = None,
        to_device_messages: Collection[JsonMapping] | None = None,
        device_list_summary: DeviceListUpdates | None = None,
    ) -> None:
        """
        Enqueue some data to be sent off to an application service.

        Args:
            appservice: The application service to create and send a transaction to.
            events: The persistent room events to send.
            ephemeral: The ephemeral events to send.
            to_device_messages: The to-device messages to send. These differ from normal
                to-device messages sent to clients, as they have 'to_device_id' and
                'to_user_id' fields.
            device_list_summary: A summary of users that the application service either needs
                to refresh the device lists of, or those that the application service need no
                longer track the device lists of.
        """
        # We purposefully allow this method to run with empty events/ephemeral
        # collections, so that callers do not need to check iterable size themselves.
        if (
            not events
            and not ephemeral
            and not to_device_messages
            and not device_list_summary
        ):
            return

        if events:
            self.queuer.queued_events.setdefault(appservice.id, []).extend(events)
        if ephemeral:
            self.queuer.queued_ephemeral.setdefault(appservice.id, []).extend(ephemeral)
        if to_device_messages:
            self.queuer.queued_to_device_messages.setdefault(appservice.id, []).extend(
                to_device_messages
            )
        if device_list_summary:
            self.queuer.queued_device_list_summaries.setdefault(
                appservice.id, []
            ).append(device_list_summary)

        # Kick off a new application service transaction
        self.queuer.start_background_request(appservice)


class _ServiceQueuer:
    """Queue of events waiting to be sent to appservices.

    Groups events into transactions per-appservice, and sends them on to the
    TransactionController. Makes sure that we only have one transaction in flight per
    appservice at a given time.
    """

    def __init__(self, txn_ctrl: "_TransactionController", hs: "HomeServer"):
        # dict of {service_id: [events]}
        self.queued_events: dict[str, list[EventBase]] = {}
        # dict of {service_id: [events]}
        self.queued_ephemeral: dict[str, list[JsonMapping]] = {}
        # dict of {service_id: [to_device_message_json]}
        self.queued_to_device_messages: dict[str, list[JsonMapping]] = {}
        # dict of {service_id: [device_list_summary]}
        self.queued_device_list_summaries: dict[str, list[DeviceListUpdates]] = {}

        # the appservices which currently have a transaction in flight
        self.requests_in_flight: set[str] = set()
        self.txn_ctrl = txn_ctrl
        self._msc3202_transaction_extensions_enabled: bool = (
            hs.config.experimental.msc3202_transaction_extensions
        )
        self.server_name = hs.hostname
        self.clock = hs.get_clock()
        self.hs = hs
        self._store = hs.get_datastores().main

        # IXO-PATCH(as-to-device): to-device messages stay in `device_inbox`, so
        # instead of storing the appservice's to-device stream position when they are
        # read (and losing them if the transaction is not delivered), the position is
        # stored once they have been delivered. Per service id:
        #
        # the to-device stream position up to which messages have been read and
        # queued in this process (absent: read from the stored position);
        self.to_device_queued_pos: dict[str, int] = {}
        # bumped whenever undelivered to-device messages are dropped, so that a read
        # which was running at that moment is discarded;
        self.to_device_generation: dict[str, int] = {}
        # the to-device stream position this process last stored in the database.
        self._to_device_stored_pos: dict[str, int] = {}

    def start_background_request(self, service: ApplicationService) -> None:
        # start a sender for this appservice if we don't already have one
        if service.id in self.requests_in_flight:
            return

        self.hs.run_as_background_process("as-sender", self._send_request, service)

    async def _send_request(self, service: ApplicationService) -> None:
        # sanity-check: we shouldn't get here if this service already has a sender
        # running.
        assert service.id not in self.requests_in_flight

        self.requests_in_flight.add(service.id)
        try:
            while True:
                all_events = self.queued_events.get(service.id, [])
                events = all_events[:MAX_PERSISTENT_EVENTS_PER_TRANSACTION]
                del all_events[:MAX_PERSISTENT_EVENTS_PER_TRANSACTION]

                all_events_ephemeral = self.queued_ephemeral.get(service.id, [])
                ephemeral = all_events_ephemeral[:MAX_EPHEMERAL_EVENTS_PER_TRANSACTION]
                del all_events_ephemeral[:MAX_EPHEMERAL_EVENTS_PER_TRANSACTION]

                all_to_device_messages = self.queued_to_device_messages.get(
                    service.id, []
                )
                to_device_messages_to_send = all_to_device_messages[
                    :MAX_TO_DEVICE_MESSAGES_PER_TRANSACTION
                ]
                del all_to_device_messages[:MAX_TO_DEVICE_MESSAGES_PER_TRANSACTION]
                # IXO-PATCH(as-to-device): when this transaction carries the last of the
                # queued to-device messages, the queued position can be stored once it
                # is delivered.
                to_device_pos = (
                    None
                    if all_to_device_messages
                    else self.to_device_queued_pos.get(service.id)
                )

                # Consolidate any pending device list summaries into a single, up-to-date
                # summary.
                # Note: this code assumes that in a single DeviceListUpdates, a user will
                # never be in both "changed" and "left" sets.
                device_list_summary = DeviceListUpdates()
                for summary in self.queued_device_list_summaries.get(service.id, []):
                    # For every user in the incoming "changed" set:
                    #   * Remove them from the existing "left" set if necessary
                    #     (as we need to start tracking them again)
                    #   * Add them to the existing "changed" set if necessary.
                    device_list_summary.left.difference_update(summary.changed)
                    device_list_summary.changed.update(summary.changed)

                    # For every user in the incoming "left" set:
                    #   * Remove them from the existing "changed" set if necessary
                    #     (we no longer need to track them)
                    #   * Add them to the existing "left" set if necessary.
                    device_list_summary.changed.difference_update(summary.left)
                    device_list_summary.left.update(summary.left)
                self.queued_device_list_summaries.clear()

                if (
                    not events
                    and not ephemeral
                    and not to_device_messages_to_send
                    # DeviceListUpdates is True if either the 'changed' or 'left' sets have
                    # at least one entry, otherwise False
                    and not device_list_summary
                ):
                    # IXO-PATCH(as-to-device): every to-device message queued so far
                    # has been delivered, so the queued position can be stored.
                    await self._store_to_device_pos(service, to_device_pos)
                    return

                one_time_keys_count: TransactionOneTimeKeysCount | None = None
                unused_fallback_keys: TransactionUnusedFallbackKeys | None = None

                delivered = False
                try:
                    if (
                        self._msc3202_transaction_extensions_enabled
                        and service.msc3202_transaction_extensions
                    ):
                        # Compute the one-time key counts and fallback key usage states
                        # for the users which are mentioned in this transaction,
                        # as well as the appservice's sender.
                        (
                            one_time_keys_count,
                            unused_fallback_keys,
                        ) = await self._compute_msc3202_otk_counts_and_fallback_keys(
                            service, events, ephemeral, to_device_messages_to_send
                        )

                    delivered = await self.txn_ctrl.send(
                        service,
                        events,
                        ephemeral,
                        to_device_messages_to_send,
                        one_time_keys_count,
                        unused_fallback_keys,
                        device_list_summary,
                    )
                except Exception:
                    logger.exception("AS request failed")

                # IXO-PATCH(as-to-device): store the to-device position only after
                # delivery; if to-device messages were not delivered, read them again
                # from the stored position instead of losing them.
                if delivered:
                    await self._store_to_device_pos(service, to_device_pos)
                    if (
                        to_device_messages_to_send
                        and to_device_pos is not None
                        and to_device_pos < self._store.get_to_device_stream_token()
                    ):
                        # The read may have stopped early: fetch the next batch.
                        self.hs.get_application_service_handler().notify_to_device_catch_up(
                            service
                        )
                elif to_device_messages_to_send:
                    self._drop_undelivered_to_device_messages(service)
        finally:
            self.requests_in_flight.discard(service.id)

    async def _store_to_device_pos(
        self, service: ApplicationService, pos: int | None
    ) -> None:
        """IXO-PATCH(as-to-device): store the to-device stream position up to which
        all to-device messages for this service have been delivered.
        """
        if pos is None or pos <= self._to_device_stored_pos.get(service.id, 0):
            return
        try:
            await self._store.set_appservice_stream_type_pos(service, "to_device", pos)
        except Exception:
            # Not stored: the messages may be delivered again after a restart.
            logger.exception(
                "Failed to store the to-device stream position for AS ID %s",
                service.id,
            )
            return
        self._to_device_stored_pos[service.id] = pos

    def _drop_undelivered_to_device_messages(self, service: ApplicationService) -> None:
        """IXO-PATCH(as-to-device): to-device messages for this service were not
        delivered. Forget everything queued after the stored position, so that the
        next read starts again from there (the messages are still in `device_inbox`).
        """
        dropped = len(self.queued_to_device_messages.pop(service.id, []))
        self.to_device_queued_pos.pop(service.id, None)
        self.to_device_generation[service.id] = (
            self.to_device_generation.get(service.id, 0) + 1
        )
        logger.info(
            "To-device messages for AS ID %s were not delivered (%i more were queued): "
            "they will be read again from device_inbox",
            service.id,
            dropped,
        )

    async def _compute_msc3202_otk_counts_and_fallback_keys(
        self,
        service: ApplicationService,
        events: Iterable[EventBase],
        ephemerals: Iterable[JsonMapping],
        to_device_messages: Iterable[JsonMapping],
    ) -> tuple[TransactionOneTimeKeysCount, TransactionUnusedFallbackKeys]:
        """
        Given a list of the events, ephemeral messages and to-device messages,
        - first computes a list of application services users that may have
          interesting updates to the one-time key counts or fallback key usage.
        - then computes one-time key counts and fallback key usages for those users.
        Given a list of application service users that are interesting,
        compute one-time key counts and fallback key usages for the users.
        """

        # Set of 'interesting' users who may have updates
        users: set[str] = set()

        # The sender is always included
        users.add(service.sender.to_string())

        # All AS users that would receive the PDUs or EDUs sent to these rooms
        # are classed as 'interesting'.
        rooms_of_interesting_users: set[str] = set()
        # PDUs
        rooms_of_interesting_users.update(event.room_id for event in events)
        # EDUs
        rooms_of_interesting_users.update(
            ephemeral["room_id"]
            for ephemeral in ephemerals
            if ephemeral.get("room_id") is not None
        )

        # Look up the AS users in those rooms
        for room_id in rooms_of_interesting_users:
            users.update(
                await self._store.get_app_service_users_in_room(room_id, service)
            )

        # Add recipients of to-device messages.
        users.update(
            device_message["to_user_id"] for device_message in to_device_messages
        )

        # Compute and return the counts / fallback key usage states
        otk_counts = await self._store.count_bulk_e2e_one_time_keys_for_as(users)
        unused_fbks = await self._store.get_e2e_bulk_unused_fallback_key_types(users)
        return otk_counts, unused_fbks


class _TransactionController:
    """Transaction manager.

    Builds AppServiceTransactions and runs their lifecycle. Also starts a Recoverer
    if a transaction fails.

    (Note we have only have one of these in the homeserver.)
    """

    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname
        self.clock = hs.get_clock()
        self.hs = hs
        self.store = hs.get_datastores().main
        self.as_api = hs.get_application_service_api()

        # map from service id to recoverer instance
        self.recoverers: dict[str, "_Recoverer"] = {}

        # for UTs
        self.RECOVERER_CLASS = _Recoverer

    async def send(
        self,
        service: ApplicationService,
        events: Sequence[EventBase],
        ephemeral: list[JsonMapping] | None = None,
        to_device_messages: list[JsonMapping] | None = None,
        one_time_keys_count: TransactionOneTimeKeysCount | None = None,
        unused_fallback_keys: TransactionUnusedFallbackKeys | None = None,
        device_list_summary: DeviceListUpdates | None = None,
    ) -> bool:
        """
        Create a transaction with the given data and send to the provided
        application service.

        Args:
            service: The application service to send the transaction to.
            events: The persistent events to include in the transaction.
            ephemeral: The ephemeral events to include in the transaction.
            to_device_messages: The to-device messages to include in the transaction.
            one_time_keys_count: Counts of remaining one-time keys for relevant
                appservice devices in the transaction.
            unused_fallback_keys: Lists of unused fallback keys for relevant
                appservice devices in the transaction.
            device_list_summary: The device list summary to include in the transaction.

        Returns:
            IXO-PATCH(as-to-device): True if the transaction was delivered to the
            application service, False otherwise (including when the service is down
            and the transaction was stored or dropped instead).
        """
        try:
            state = await self.store.get_appservice_state(service)
            service_is_up = state == ApplicationServiceState.UP or state is None

            # IXO-PATCH(as-self-heal): the service may have been marked DOWN by
            # another process (for example the old pod during a rolling restart).
            # Nothing in this process would ever retry it: start a recoverer here.
            if state == ApplicationServiceState.DOWN and not self.is_recovering(
                service
            ):
                logger.warning(
                    "AS ID %s is marked down but this process has no recoverer for it",
                    service.id,
                )
                self.start_recoverer(service)

            # Don't create empty txns when in recovery mode (ephemeral events are dropped)
            if not service_is_up and not events:
                return False

            txn = await self.store.create_appservice_txn(
                service=service,
                events=events,
                ephemeral=ephemeral or [],
                to_device_messages=to_device_messages or [],
                one_time_keys_count=one_time_keys_count or {},
                unused_fallback_keys=unused_fallback_keys or {},
                device_list_summary=device_list_summary or DeviceListUpdates(),
            )
            if service_is_up:
                sent = await txn.send(self.as_api)
                if sent:
                    await txn.complete(self.store)
                    return True
                else:
                    run_in_background(self._on_txn_fail, service)
        except Exception:
            logger.exception("Error creating appservice transaction")
            run_in_background(self._on_txn_fail, service)
        return False

    async def on_recovered(self, recoverer: "_Recoverer") -> None:
        logger.info(
            "Successfully recovered application service AS ID %s", recoverer.service.id
        )
        self.recoverers.pop(recoverer.service.id)
        logger.info("Remaining active recoverers: %s", len(self.recoverers))
        await self.store.set_appservice_state(
            recoverer.service, ApplicationServiceState.UP
        )

        # IXO-PATCH(as-to-device): stored transactions only keep room events. The
        # to-device messages that could not be delivered while the service was down
        # are still in `device_inbox` after the stored position: read them again.
        self.hs.get_application_service_handler().notify_to_device_catch_up(
            recoverer.service
        )

    async def _on_txn_fail(self, service: ApplicationService) -> None:
        try:
            await self.store.set_appservice_state(service, ApplicationServiceState.DOWN)
            self.start_recoverer(service)
        except Exception:
            logger.exception("Error starting AS recoverer")

    def start_recoverer(self, service: ApplicationService) -> None:
        """Start a Recoverer for the given service

        Args:
            service:
        """
        # IXO-PATCH(as-self-heal): idempotent. A failed send, `start()`, `send()`
        # and the periodic check can all ask for a recoverer for the same service
        # (upstream #20454 / #20458 asserted here).
        if self.is_recovering(service):
            logger.debug("AS ID %s already has a recoverer", service.id)
            return

        logger.info("Starting recoverer for AS ID %s", service.id)
        recoverer = self.RECOVERER_CLASS(
            self.server_name,
            self.clock,
            self.hs,
            self.store,
            self.as_api,
            service,
            self.on_recovered,
        )
        self.recoverers[service.id] = recoverer
        recoverer.recover()
        logger.info("Now %i active recoverers", len(self.recoverers))

    def force_retry(self, service: ApplicationService) -> None:
        """Forces a Recoverer to attempt delivery of transations immediately.

        Args:
            service:
        """
        recoverer = self.recoverers.get(service.id)
        if not recoverer:
            # No need to force a retry on a happy AS.
            logger.info("%s is not in recovery, not forcing retry", service.id)
            return

        recoverer.force_retry()

    def is_recovering(self, service: ApplicationService) -> bool:
        """IXO-PATCH(as-self-heal): whether this process has a recoverer for the
        given service.
        """
        return service.id in self.recoverers


class _Recoverer:
    """Manages retries and backoff for a DOWN appservice.

    We have one of these for each appservice which is currently considered DOWN.

    Args:
        server_name: the homeserver name (used to label metrics) (this should be `hs.hostname`).
        clock:
        store:
        as_api:
        service: the service we are managing
        callback: called once the service recovers.
    """

    def __init__(
        self,
        server_name: str,
        clock: Clock,
        hs: "HomeServer",
        store: DataStore,
        as_api: ApplicationServiceApi,
        service: ApplicationService,
        callback: Callable[["_Recoverer"], Awaitable[None]],
    ):
        self.server_name = server_name
        self.clock = clock
        self.hs = hs
        self.store = store
        self.as_api = as_api
        self.service = service
        self.callback = callback
        self.backoff_counter = 1
        self.scheduled_recovery: DelayedCallWrapper | None = None

    def recover(self) -> None:
        delay = Duration(seconds=2**self.backoff_counter)
        logger.info("Scheduling retries on %s in %fs", self.service.id, delay.as_secs())
        self.scheduled_recovery = self.clock.call_later(
            delay,
            self.hs.run_as_background_process,
            "as-recoverer",
            self.retry,
        )

    def _backoff(self) -> None:
        # cap the backoff to be around 8.5min => (2^9) = 512 secs
        if self.backoff_counter < 9:
            self.backoff_counter += 1
        self.recover()

    def force_retry(self) -> None:
        """Cancels the existing timer and forces an immediate retry in the background.

        Args:
            service:
        """
        # Prevent the existing backoff from occuring
        if self.scheduled_recovery:
            self.clock.cancel_call_later(self.scheduled_recovery)
        # Run a retry, which will resechedule a recovery if it fails.
        self.hs.run_as_background_process(
            "retry",
            self.retry,
        )

    async def retry(self) -> None:
        logger.info("Starting retries on %s", self.service.id)
        try:
            while True:
                txn = await self.store.get_oldest_unsent_txn(self.service)
                if not txn:
                    # nothing left: we're done!
                    await self.callback(self)
                    return

                logger.info(
                    "Retrying transaction %s for AS ID %s", txn.id, txn.service.id
                )
                sent = await txn.send(self.as_api)
                if not sent:
                    break

                await txn.complete(self.store)

                # reset the backoff counter and then process the next transaction
                self.backoff_counter = 1

        except Exception:
            logger.exception("Unexpected error running retries")

        # we didn't manage to send all of the transactions before we got an error of
        # some flavour: reschedule the next retry.
        self._backoff()
