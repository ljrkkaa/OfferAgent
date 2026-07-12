import time

import pytest

from khoj.database.adapters import ProcessLockAdapters
from khoj.database.models import ProcessLock
from tests.helpers import ProcessLockFactory


@pytest.mark.django_db(transaction=True)
def test_process_lock(default_process_lock):
    # Arrange
    lock: ProcessLock = default_process_lock

    # Assert
    assert ProcessLockAdapters.is_process_locked_by_name(lock.name)


@pytest.mark.django_db(transaction=True)
def test_expired_process_lock():
    # Arrange
    lock: ProcessLock = ProcessLockFactory(name="test_expired_lock", max_duration_in_seconds=2)

    # Act
    time.sleep(3)

    # Assert
    assert not ProcessLockAdapters.is_process_locked_by_name(lock.name)


@pytest.mark.django_db(transaction=True)
def test_get_process_lock_discards_expired_lock():
    lock = ProcessLockFactory(name=ProcessLock.Operation.SCHEDULE_LEADER, max_duration_in_seconds=-1)

    assert ProcessLockAdapters.get_process_lock(lock.name) is None
    assert not ProcessLock.objects.filter(pk=lock.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_in_progress_lock(default_process_lock):
    # Arrange
    lock: ProcessLock = default_process_lock

    # Act
    ProcessLockAdapters.run_with_lock(lock.name, lambda: time.sleep(2))

    # Assert
    assert ProcessLockAdapters.is_process_locked_by_name(lock.name)


@pytest.mark.django_db(transaction=True)
def test_run_with_completed():
    # Arrange
    ProcessLockAdapters.run_with_lock("test_run_with", lambda: time.sleep(2))

    # Act
    time.sleep(4)

    # Assert
    assert not ProcessLockAdapters.is_process_locked_by_name("test_run_with")


@pytest.mark.django_db(transaction=True)
def test_nonexistent_lock():
    # Assert
    assert not ProcessLockAdapters.is_process_locked_by_name("nonexistent_lock")
