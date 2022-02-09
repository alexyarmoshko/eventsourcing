import os
import traceback
import zlib
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from tempfile import NamedTemporaryFile
from threading import Event, Thread, get_ident
from time import sleep
from timeit import timeit
from typing import Any, Dict, List, Optional
from unittest import TestCase
from uuid import uuid4

from eventsourcing.cipher import AESCipher
from eventsourcing.compressor import ZlibCompressor
from eventsourcing.domain import DomainEvent
from eventsourcing.persistence import (
    AggregateRecorder,
    ApplicationRecorder,
    DatetimeAsISO,
    DecimalAsStr,
    InfrastructureFactory,
    IntegrityError,
    JSONTranscoder,
    Mapper,
    ProcessRecorder,
    StoredEvent,
    Tracking,
    UUIDAsHex,
)
from eventsourcing.utils import Environment, get_topic


class AggregateRecorderTestCase(TestCase, ABC):
    @abstractmethod
    def create_recorder(self) -> AggregateRecorder:
        """"""

    def test_insert_and_select(self) -> None:

        # Construct the recorder.
        recorder = self.create_recorder()

        # Check we can call insert_events() with an empty list.
        notification_ids = recorder.insert_events([])
        self.assertEqual(notification_ids, None)

        # Select stored events, expect empty list.
        originator_id1 = uuid4()
        self.assertEqual(
            recorder.select_events(originator_id1, desc=True, limit=1),
            [],
        )

        # Write a stored event.
        stored_event1 = StoredEvent(
            originator_id=originator_id1,
            originator_version=0,
            topic="topic1",
            state=b"state1",
        )
        notification_ids = recorder.insert_events([stored_event1])
        self.assertEqual(notification_ids, None)

        # Select stored events, expect list of one.
        stored_events = recorder.select_events(originator_id1)
        self.assertEqual(len(stored_events), 1)
        assert stored_events[0].originator_id == originator_id1
        assert stored_events[0].originator_version == 0
        assert stored_events[0].topic == "topic1"

        # Check get record conflict error if attempt to store it again.
        stored_events = recorder.select_events(originator_id1)
        with self.assertRaises(IntegrityError):
            recorder.insert_events([stored_event1])

        # Check writing of events is atomic.
        stored_event2 = StoredEvent(
            originator_id=originator_id1,
            originator_version=1,
            topic="topic2",
            state=b"state2",
        )
        with self.assertRaises(IntegrityError):
            recorder.insert_events([stored_event1, stored_event2])

        with self.assertRaises(IntegrityError):
            recorder.insert_events([stored_event2, stored_event2])

        # Check still only have one record.
        stored_events = recorder.select_events(originator_id1)
        self.assertEqual(len(stored_events), 1)
        assert stored_events[0].originator_id == originator_id1
        assert stored_events[0].originator_version == 0
        assert stored_events[0].topic == "topic1"

        # Check can write two events together.
        stored_event3 = StoredEvent(
            originator_id=originator_id1,
            originator_version=2,
            topic="topic3",
            state=b"state3",
        )
        notification_ids = recorder.insert_events([stored_event2, stored_event3])
        self.assertEqual(notification_ids, None)

        # Check we got what was written.
        stored_events = recorder.select_events(originator_id1)
        self.assertEqual(len(stored_events), 3)
        assert stored_events[0].originator_id == originator_id1
        assert stored_events[0].originator_version == 0
        assert stored_events[0].topic == "topic1"
        self.assertEqual(stored_events[0].state, b"state1")
        assert stored_events[1].originator_id == originator_id1
        assert stored_events[1].originator_version == 1
        assert stored_events[1].topic == "topic2"
        assert stored_events[1].state == b"state2"
        assert stored_events[2].originator_id == originator_id1
        assert stored_events[2].originator_version == 2
        assert stored_events[2].topic == "topic3"
        assert stored_events[2].state == b"state3"

        # Check we can get the last one recorded (used to get last snapshot).
        events = recorder.select_events(originator_id1, desc=True, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0],
            stored_event3,
        )

        # Check we can get the last one before a particular version.
        events = recorder.select_events(originator_id1, lte=1, desc=True, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0],
            stored_event2,
        )

        # Check we can get events between particular versions.
        events = recorder.select_events(originator_id1, gt=0, lte=2)
        self.assertEqual(len(events), 2)
        self.assertEqual(
            events[0],
            stored_event2,
        )
        self.assertEqual(
            events[1],
            stored_event3,
        )

        # Check aggregate sequences are distinguished.
        originator_id2 = uuid4()
        self.assertEqual(
            recorder.select_events(originator_id2),
            [],
        )

        # Write a stored event.
        stored_event4 = StoredEvent(
            originator_id=originator_id2,
            originator_version=0,
            topic="topic4",
            state=b"state4",
        )
        recorder.insert_events([stored_event4])
        self.assertEqual(
            recorder.select_events(originator_id2),
            [stored_event4],
        )

    def test_performance(self) -> None:

        # Construct the recorder.
        recorder = self.create_recorder()

        def insert() -> None:
            originator_id = uuid4()

            stored_event = StoredEvent(
                originator_id=originator_id,
                originator_version=0,
                topic="topic1",
                state=b"state1",
            )
            recorder.insert_events([stored_event])

        # Warm up.
        number = 10
        timeit(insert, number=number)

        number = 100
        duration = timeit(insert, number=number)
        print(self, f"\n{1000000 * duration / number:.1f}μs per insert")


class ApplicationRecorderTestCase(TestCase, ABC):
    @abstractmethod
    def create_recorder(self) -> ApplicationRecorder:
        """"""

    def test_insert_select(self) -> None:
        # Construct the recorder.
        recorder = self.create_recorder()

        # Check notifications methods work when there aren't any.
        self.assertEqual(
            recorder.max_notification_id(),
            0,
        )
        self.assertEqual(
            len(recorder.select_notifications(1, 3)),
            0,
        )
        self.assertEqual(
            len(recorder.select_notifications(1, 3, topics=["topic1"])),
            0,
        )

        # Write two stored events.
        originator_id1 = uuid4()
        originator_id2 = uuid4()

        stored_event1 = StoredEvent(
            originator_id=originator_id1,
            originator_version=0,
            topic="topic1",
            state=b"state1",
        )
        stored_event2 = StoredEvent(
            originator_id=originator_id1,
            originator_version=1,
            topic="topic2",
            state=b"state2",
        )
        stored_event3 = StoredEvent(
            originator_id=originator_id2,
            originator_version=1,
            topic="topic3",
            state=b"state3",
        )

        notification_ids = recorder.insert_events([])
        self.assertEqual(notification_ids, [])

        notification_ids = recorder.insert_events([stored_event1, stored_event2])
        self.assertEqual(notification_ids, [1, 2])

        notification_ids = recorder.insert_events([stored_event3])
        self.assertEqual(notification_ids, [3])

        stored_events1 = recorder.select_events(originator_id1)
        stored_events2 = recorder.select_events(originator_id2)

        # Check we got what was written.
        self.assertEqual(len(stored_events1), 2)
        self.assertEqual(len(stored_events2), 1)

        notifications = recorder.select_notifications(1, 3)
        self.assertEqual(len(notifications), 3)
        self.assertEqual(notifications[0].id, 1)
        self.assertEqual(notifications[0].originator_id, originator_id1)
        self.assertEqual(notifications[0].topic, "topic1")
        self.assertEqual(notifications[0].state, b"state1")
        self.assertEqual(notifications[1].id, 2)
        self.assertEqual(notifications[1].originator_id, originator_id1)
        self.assertEqual(notifications[1].topic, "topic2")
        self.assertEqual(notifications[1].state, b"state2")
        self.assertEqual(notifications[2].id, 3)
        self.assertEqual(notifications[2].originator_id, originator_id2)
        self.assertEqual(notifications[2].topic, "topic3")
        self.assertEqual(notifications[2].state, b"state3")

        notifications = recorder.select_notifications(
            1, 3, topics=["topic1", "topic2", "topic3"]
        )
        self.assertEqual(len(notifications), 3)
        self.assertEqual(notifications[0].id, 1)
        self.assertEqual(notifications[0].originator_id, originator_id1)
        self.assertEqual(notifications[0].topic, "topic1")
        self.assertEqual(notifications[0].state, b"state1")
        self.assertEqual(notifications[1].id, 2)
        self.assertEqual(notifications[1].originator_id, originator_id1)
        self.assertEqual(notifications[1].topic, "topic2")
        self.assertEqual(notifications[1].state, b"state2")
        self.assertEqual(notifications[2].id, 3)
        self.assertEqual(notifications[2].originator_id, originator_id2)
        self.assertEqual(notifications[2].topic, "topic3")
        self.assertEqual(notifications[2].state, b"state3")

        notifications = recorder.select_notifications(1, 3, topics=["topic1"])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].id, 1)
        self.assertEqual(notifications[0].originator_id, originator_id1)
        self.assertEqual(notifications[0].topic, "topic1")
        self.assertEqual(notifications[0].state, b"state1")

        notifications = recorder.select_notifications(1, 3, topics=["topic2"])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].id, 2)
        self.assertEqual(notifications[0].originator_id, originator_id1)
        self.assertEqual(notifications[0].topic, "topic2")
        self.assertEqual(notifications[0].state, b"state2")

        notifications = recorder.select_notifications(1, 3, topics=["topic3"])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].id, 3)
        self.assertEqual(notifications[0].originator_id, originator_id2)
        self.assertEqual(notifications[0].topic, "topic3")
        self.assertEqual(notifications[0].state, b"state3")

        notifications = recorder.select_notifications(1, 3, topics=["topic1", "topic3"])
        self.assertEqual(len(notifications), 2)
        self.assertEqual(notifications[0].id, 1)
        self.assertEqual(notifications[0].originator_id, originator_id1)
        self.assertEqual(notifications[0].topic, "topic1")
        self.assertEqual(notifications[0].state, b"state1")
        self.assertEqual(notifications[1].id, 3)
        self.assertEqual(notifications[1].topic, "topic3")
        self.assertEqual(notifications[1].state, b"state3")

        self.assertEqual(
            recorder.max_notification_id(),
            3,
        )

        notifications = recorder.select_notifications(1, 1)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].id, 1)

        notifications = recorder.select_notifications(2, 1)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].id, 2)

        notifications = recorder.select_notifications(2, 2)
        self.assertEqual(len(notifications), 2)
        self.assertEqual(notifications[0].id, 2)
        self.assertEqual(notifications[1].id, 3)

        notifications = recorder.select_notifications(3, 1)
        self.assertEqual(len(notifications), 1, len(notifications))
        self.assertEqual(notifications[0].id, 3)

        notifications = recorder.select_notifications(start=2, limit=10, stop=2)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].id, 2)

        notifications = recorder.select_notifications(start=1, limit=10, stop=2)
        self.assertEqual(len(notifications), 2, len(notifications))
        self.assertEqual(notifications[0].id, 1)
        self.assertEqual(notifications[1].id, 2)

    def test_concurrent_no_conflicts(self) -> None:
        print(self)

        recorder = self.create_recorder()

        errors_happened = Event()
        errors: List[Exception] = []

        counts = {}
        threads: Dict[int, int] = {}
        durations: Dict[int, float] = {}

        num_writers = 10
        num_writes_per_writer = 100
        num_events_per_write = 100
        reader_sleep = 0.0
        writer_sleep = 0.0

        def insert_events() -> None:
            thread_id = get_ident()
            if thread_id not in threads:
                threads[thread_id] = len(threads)
            if thread_id not in counts:
                counts[thread_id] = 0
            if thread_id not in durations:
                durations[thread_id] = 0

            # thread_num = threads[thread_id]
            # count = counts[thread_id]

            originator_id = uuid4()
            stored_events = [
                StoredEvent(
                    originator_id=originator_id,
                    originator_version=i,
                    topic="topic",
                    state=b"state",
                )
                for i in range(num_events_per_write)
            ]
            started = datetime.now()
            # print(f"Thread {thread_num} write beginning #{count + 1}")
            try:
                recorder.insert_events(stored_events)

            except Exception as e:  # pragma: nocover
                if errors:
                    return
                ended = datetime.now()
                duration = (ended - started).total_seconds()
                print(f"Error after starting {duration}", e)
                errors.append(e)
            else:
                ended = datetime.now()
                duration = (ended - started).total_seconds()
                counts[thread_id] += 1
                if duration > durations[thread_id]:
                    durations[thread_id] = duration
                sleep(writer_sleep)

        stop_reading = Event()

        def read_continuously() -> None:
            while not stop_reading.is_set():
                try:
                    recorder.select_notifications(0, 10)
                except Exception as e:  # pragma: nocover
                    errors.append(e)
                    return
                # else:
                sleep(reader_sleep)

        reader_thread1 = Thread(target=read_continuously)
        reader_thread1.start()

        reader_thread2 = Thread(target=read_continuously)
        reader_thread2.start()

        with ThreadPoolExecutor(max_workers=num_writers) as executor:
            futures = []
            for _ in range(num_writes_per_writer):
                if errors:  # pragma: nocover
                    break
                future = executor.submit(insert_events)
                futures.append(future)
            for future in futures:
                if errors:  # pragma: nocover
                    break
                try:
                    future.result()
                except Exception as e:  # pragma: nocover
                    errors.append(e)
                    break

        stop_reading.set()

        if errors:  # pragma: nocover
            raise errors[0]

        for thread_id, thread_num in threads.items():
            count = counts[thread_id]
            duration = durations[thread_id]
            print(f"Thread {thread_num} wrote {count} times (max dur {duration})")
        self.assertFalse(errors_happened.is_set())

    def test_concurrent_throughput(self) -> None:
        print(self)

        recorder = self.create_recorder()

        errors_happened = Event()

        counts = {}
        threads: Dict[int, int] = {}
        durations: Dict[int, float] = {}

        # Match this to the batch page size in postgres insert for max throughput.
        NUM_EVENTS = 500

        started = datetime.now()

        def insert_events() -> None:
            thread_id = get_ident()
            if thread_id not in threads:
                threads[thread_id] = len(threads)
            if thread_id not in counts:
                counts[thread_id] = 0
            if thread_id not in durations:
                durations[thread_id] = 0

            originator_id = uuid4()
            stored_events = [
                StoredEvent(
                    originator_id=originator_id,
                    originator_version=i,
                    topic="topic",
                    state=b"state",
                )
                for i in range(NUM_EVENTS)
            ]

            try:
                recorder.insert_events(stored_events)

            except Exception:  # pragma: nocover
                errors_happened.set()
                tb = traceback.format_exc()
                print(tb)
            finally:
                ended = datetime.now()
                duration = (ended - started).total_seconds()
                counts[thread_id] += 1
                durations[thread_id] = duration

        NUM_JOBS = 60

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for _ in range(NUM_JOBS):
                future = executor.submit(insert_events)
                # future.add_done_callback(self.close_db_connection)
                futures.append(future)
            for future in futures:
                future.result()

        self.assertFalse(errors_happened.is_set(), "There were errors (see above)")
        ended = datetime.now()
        print("Rate:", NUM_JOBS * NUM_EVENTS / (ended - started).total_seconds())

    def close_db_connection(self, *args: Any) -> None:
        """"""


class ProcessRecorderTestCase(TestCase, ABC):
    @abstractmethod
    def create_recorder(self) -> ProcessRecorder:
        """"""

    def test_insert_select(self) -> None:
        # Construct the recorder.
        recorder = self.create_recorder()

        # Get current position.
        self.assertEqual(
            recorder.max_tracking_id("upstream_app"),
            0,
        )

        # Write two stored events.
        originator_id1 = uuid4()
        originator_id2 = uuid4()

        stored_event1 = StoredEvent(
            originator_id=originator_id1,
            originator_version=1,
            topic="topic1",
            state=b"state1",
        )
        stored_event2 = StoredEvent(
            originator_id=originator_id1,
            originator_version=2,
            topic="topic2",
            state=b"state2",
        )
        stored_event3 = StoredEvent(
            originator_id=originator_id2,
            originator_version=1,
            topic="topic3",
            state=b"state3",
        )
        stored_event4 = StoredEvent(
            originator_id=originator_id2,
            originator_version=2,
            topic="topic4",
            state=b"state4",
        )
        tracking1 = Tracking(
            application_name="upstream_app",
            notification_id=1,
        )
        tracking2 = Tracking(
            application_name="upstream_app",
            notification_id=2,
        )

        # Insert two events with tracking info.
        recorder.insert_events(
            stored_events=[
                stored_event1,
                stored_event2,
            ],
            tracking=tracking1,
        )

        # Get current position.
        self.assertEqual(
            recorder.max_tracking_id("upstream_app"),
            1,
        )

        # Check can't insert third event with same tracking info.
        with self.assertRaises(IntegrityError):
            recorder.insert_events(
                stored_events=[stored_event3],
                tracking=tracking1,
            )

        # Get current position.
        self.assertEqual(
            recorder.max_tracking_id("upstream_app"),
            1,
        )

        # Insert third event with different tracking info.
        recorder.insert_events(
            stored_events=[stored_event3],
            tracking=tracking2,
        )

        # Get current position.
        self.assertEqual(
            recorder.max_tracking_id("upstream_app"),
            2,
        )

        # Insert fourth event without tracking info.
        recorder.insert_events(
            stored_events=[stored_event4],
        )

        # Get current position.
        self.assertEqual(
            recorder.max_tracking_id("upstream_app"),
            2,
        )

    def test_performance(self) -> None:

        # Construct the recorder.
        recorder = self.create_recorder()

        number = 100

        notification_ids = iter(range(1, number + 1))

        def insert_events() -> None:
            originator_id = uuid4()

            stored_event = StoredEvent(
                originator_id=originator_id,
                originator_version=0,
                topic="topic1",
                state=b"state1",
            )
            tracking1 = Tracking(
                application_name="upstream_app",
                notification_id=next(notification_ids),
            )

            recorder.insert_events(
                stored_events=[
                    stored_event,
                ],
                tracking=tracking1,
            )

        duration = timeit(insert_events, number=number)
        print(self, f"{duration / number:.9f}")


class NonInterleavingNotificationIDsBaseCase(ABC, TestCase):
    insert_num = 1000

    def test(self):
        recorder = self.create_recorder()

        race_started = Event()

        originator1_id = uuid4()
        originator2_id = uuid4()

        stack1 = self.create_stack(originator1_id)
        stack2 = self.create_stack(originator2_id)

        errors = []

        def insert_stack(stack):
            try:
                race_started.wait()
                recorder.insert_events(stack)
            except Exception as e:
                errors.append(e)

        thread1 = Thread(target=insert_stack, args=(stack1,), daemon=True)
        thread2 = Thread(target=insert_stack, args=(stack2,), daemon=True)

        thread1.start()
        thread2.start()

        race_started.set()

        thread1.join()
        thread2.join()

        if errors:
            raise errors[0]

        notifications = recorder.select_notifications(start=1, limit=1000000)
        ids_for_sequence1 = [
            e.id for e in notifications if e.originator_id == originator1_id
        ]
        ids_for_sequence2 = [
            e.id for e in notifications if e.originator_id == originator2_id
        ]
        max_id_for_sequence1 = max(ids_for_sequence1)
        max_id_for_sequence2 = max(ids_for_sequence2)
        min_id_for_sequence1 = min(ids_for_sequence1)
        min_id_for_sequence2 = min(ids_for_sequence2)

        if max_id_for_sequence1 > min_id_for_sequence2:
            self.assertGreater(min_id_for_sequence1, max_id_for_sequence2)
        else:
            self.assertGreater(min_id_for_sequence2, max_id_for_sequence1)

    def create_stack(self, originator_id):
        return [
            StoredEvent(
                originator_id=originator_id,
                originator_version=i,
                topic="",
                state=b"",
            )
            for i in range(self.insert_num)
        ]

    @abstractmethod
    def create_recorder(self) -> ApplicationRecorder:
        pass


class InfrastructureFactoryTestCase(ABC, TestCase):
    env: Optional[Environment] = None

    @abstractmethod
    def expected_factory_class(self):
        pass

    @abstractmethod
    def expected_aggregate_recorder_class(self):
        pass

    @abstractmethod
    def expected_application_recorder_class(self):
        pass

    @abstractmethod
    def expected_process_recorder_class(self):
        pass

    def setUp(self) -> None:
        self.factory = InfrastructureFactory.construct(self.env)
        self.assertIsInstance(self.factory, self.expected_factory_class())
        self.transcoder = JSONTranscoder()
        self.transcoder.register(UUIDAsHex())
        self.transcoder.register(DecimalAsStr())
        self.transcoder.register(DatetimeAsISO())

    def test_createmapper(self):

        # Want to construct:
        #  - application recorder
        #  - snapshot recorder
        #  - mapper
        #  - event store
        #  - snapshot store

        # Want to make configurable:
        #  - cipher (and cipher key)
        #  - compressor
        #  - application recorder class (and db uri, and session)
        #  - snapshot recorder class (and db uri, and session)

        # Common environment:
        #  - factory topic
        #  - cipher topic
        #  - cipher key
        #  - compressor topic

        # POPO environment:

        # SQLite environment:
        #  - database topic
        #  - table name for stored events
        #  - table name for snapshots

        # Create mapper.

        mapper = self.factory.mapper(
            transcoder=self.transcoder,
        )
        self.assertIsInstance(mapper, Mapper)
        self.assertIsNone(mapper.cipher)
        self.assertIsNone(mapper.compressor)

    def test_createmapper_with_compressor(self):

        # Create mapper with compressor class as topic.
        self.env[self.factory.COMPRESSOR_TOPIC] = get_topic(ZlibCompressor)
        mapper = self.factory.mapper(transcoder=self.transcoder)
        self.assertIsInstance(mapper, Mapper)
        self.assertIsInstance(mapper.compressor, ZlibCompressor)
        self.assertIsNone(mapper.cipher)

        # Create mapper with compressor module as topic.
        self.env[self.factory.COMPRESSOR_TOPIC] = "zlib"
        mapper = self.factory.mapper(transcoder=self.transcoder)
        self.assertIsInstance(mapper, Mapper)
        self.assertEqual(mapper.compressor, zlib)
        self.assertIsNone(mapper.cipher)

    def test_createmapper_with_cipher(self):

        # Check cipher needs a key.
        self.env[self.factory.CIPHER_TOPIC] = get_topic(AESCipher)

        with self.assertRaises(EnvironmentError):
            self.factory.mapper(transcoder=self.transcoder)

        # Check setting key but no topic defers to AES.
        del self.env[self.factory.CIPHER_TOPIC]

        cipher_key = AESCipher.create_key(16)
        self.env[AESCipher.CIPHER_KEY] = cipher_key

        # Create mapper with cipher.
        mapper = self.factory.mapper(transcoder=self.transcoder)
        self.assertIsInstance(mapper, Mapper)
        self.assertIsNotNone(mapper.cipher)
        self.assertIsNone(mapper.compressor)

    def test_createmapper_with_cipher_and_compressor(
        self,
    ):

        # Create mapper with cipher and compressor.
        self.env[self.factory.COMPRESSOR_TOPIC] = get_topic(ZlibCompressor)

        self.env[self.factory.CIPHER_TOPIC] = get_topic(AESCipher)
        cipher_key = AESCipher.create_key(16)
        self.env[AESCipher.CIPHER_KEY] = cipher_key

        mapper = self.factory.mapper(transcoder=self.transcoder)
        self.assertIsInstance(mapper, Mapper)
        self.assertIsNotNone(mapper.cipher)
        self.assertIsNotNone(mapper.compressor)

    def test_mapper_with_wrong_cipher_key(self):
        self.env.name = "App1"
        self.env[self.factory.CIPHER_TOPIC] = get_topic(AESCipher)
        cipher_key1 = AESCipher.create_key(16)
        cipher_key2 = AESCipher.create_key(16)
        self.env["APP1_" + AESCipher.CIPHER_KEY] = cipher_key1
        self.env["APP2_" + AESCipher.CIPHER_KEY] = cipher_key2

        mapper1: Mapper = self.factory.mapper(
            transcoder=self.transcoder,
        )

        domain_event = DomainEvent(
            originator_id=uuid4(),
            originator_version=1,
            timestamp=DomainEvent.create_timestamp(),
        )
        stored_event = mapper1.from_domain_event(domain_event)
        copy = mapper1.to_domain_event(stored_event)
        self.assertEqual(domain_event.originator_id, copy.originator_id)

        self.env.name = "App2"
        mapper2: Mapper = self.factory.mapper(
            transcoder=self.transcoder,
        )
        # This should fail because the infrastructure factory
        # should read different cipher keys from the environment.
        with self.assertRaises(ValueError):
            mapper2.to_domain_event(stored_event)

    def test_create_aggregate_recorder(self):
        recorder = self.factory.aggregate_recorder()
        self.assertEqual(type(recorder), self.expected_aggregate_recorder_class())

        self.assertIsInstance(recorder, AggregateRecorder)

        # Exercise code path where table is not created.
        self.env["CREATE_TABLE"] = "f"
        recorder = self.factory.aggregate_recorder()
        self.assertEqual(type(recorder), self.expected_aggregate_recorder_class())

    def test_create_application_recorder(self):
        recorder = self.factory.application_recorder()
        self.assertEqual(type(recorder), self.expected_application_recorder_class())
        self.assertIsInstance(recorder, ApplicationRecorder)

        # Exercise code path where table is not created.
        self.env["CREATE_TABLE"] = "f"
        recorder = self.factory.application_recorder()
        self.assertEqual(type(recorder), self.expected_application_recorder_class())

    def test_create_process_recorder(self):
        recorder = self.factory.process_recorder()
        self.assertEqual(type(recorder), self.expected_process_recorder_class())
        self.assertIsInstance(recorder, ProcessRecorder)

        # Exercise code path where table is not created.
        self.env["CREATE_TABLE"] = "f"
        recorder = self.factory.process_recorder()
        self.assertEqual(type(recorder), self.expected_process_recorder_class())


def tmpfile_uris():
    tmp_files = []
    ram_disk_path = "/Volumes/RAM DISK/"
    prefix = None
    if os.path.exists(ram_disk_path):
        prefix = ram_disk_path
    while True:
        tmp_file = NamedTemporaryFile(prefix=prefix, suffix="_eventsourcing_test.db")
        tmp_files.append(tmp_file)
        yield "file:" + tmp_file.name
