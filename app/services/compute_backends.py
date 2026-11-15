import random
from collections.abc import Sequence
from itertools import cycle

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette import status

from app.db.models import ComputeBackend
from app.db.models.backends import BackendSelectionSettings
from app.schemas.backend import BackendSelectionMethod


def get_next_backend_for_launch(
    session: Session,
    selection_settings: BackendSelectionSettings | None,
) -> ComputeBackend:
    """
    Select the next compute backend to launch a job on, based on
    the current selection settings.
    """
    if selection_settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend selection settings are not configured.",
        )
    try:
        return select_next_backend(session, selection_settings)
    except (ValueError, IndexError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No compute backend could be selected.",
        ) from exc


def select_next_backend(session: Session, selection_settings: BackendSelectionSettings) -> ComputeBackend:
    if selection_settings.selection_method == "random":
        return get_backend_random(session)
    elif selection_settings.selection_method == "cycle":
        return get_backend_cycle(session, selection_settings)
    else:
        raise ValueError(f"Unknown selection method: {selection_settings.selection_method}")

def get_enabled_backends(session: Session) -> Sequence[ComputeBackend]:
    """
    Get currently enabled backends, ordering by name (so we have a stable ordering
    if cycling etc.)
    """
    return session.scalars(select(ComputeBackend).where(ComputeBackend.enabled).order_by(ComputeBackend.name)).all()

def get_backend_random(session: Session) -> ComputeBackend:
    enabled_backends = get_enabled_backends(session)
    return random.choice(enabled_backends)

def get_backend_cycle(session: Session, selection_settings: BackendSelectionSettings) -> ComputeBackend:
    backends = get_enabled_backends(session)
    if not backends:
        raise ValueError("No enabled compute backends")

    last_index = next(
        (
            index
            for index, backend in enumerate(backends)
            if backend.id == selection_settings.last_selected_id
        ),
        -1,
    )
    backend = backends[(last_index + 1) % len(backends)]
    selection_settings.record_selection(backend)
    session.add(selection_settings)
    return backend


