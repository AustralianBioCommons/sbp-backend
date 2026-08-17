import random
import uuid

from faker import Faker
from polyfactory.factories.dataclass_factory import DataclassFactory
from polyfactory.factories.sqlalchemy_factory import SQLAlchemyFactory

from app.db.models.core import (
    AppUser,
    DataTransfer,
    RunInput,
    RunOutput,
    S3Object,
    Workflow,
    WorkflowRun,
)
from app.db.models.job_queue import QueuedJob
from app.services.job_utils import UserJobListRow

fake = Faker()


def random_auth0_id() -> str:
    return "auth0|" + "".join(random.choices("0123456789abcdef", k=24))


def biocommons_username() -> str:
    # Must pass regex ^[-_a-z0-9]+$ and length 3–128
    # Generate with some uuid at the end to ensure randomness - was having issues
    #   with tests intermittently failing
    username = (
        fake.first_name()
        + random.choice(list("-_"))
        + fake.last_name()
        + "-"
        + uuid.uuid4().hex[:8]
    )
    return username.lower()


class AppUserFactory(SQLAlchemyFactory[AppUser]):
    __set_relationships__ = False

    @staticmethod
    def auth0_user_id() -> str:
        return random_auth0_id()

    @staticmethod
    def email() -> str:
        return fake.email()

    @staticmethod
    def name() -> str:
        return fake.name()


class WorkflowFactory(SQLAlchemyFactory[Workflow]):
    __set_relationships__ = False


class WorkflowRunFactory(SQLAlchemyFactory[WorkflowRun]):
    __set_relationships__ = False


class RunInputFactory(SQLAlchemyFactory[RunInput]):
    __set_relationships__ = False


class RunOutputFactory(SQLAlchemyFactory[RunOutput]):
    __set_relationships__ = False


class S3ObjectFactory(SQLAlchemyFactory[S3Object]):
    __set_relationships__ = False


class QueuedJobFactory(SQLAlchemyFactory[QueuedJob]):
    __set_relationships__ = False


class DataTransferFactory(SQLAlchemyFactory[DataTransfer]):
    __set_relationships__ = False


class UserJobListRowFactory(DataclassFactory[UserJobListRow]):
    @classmethod
    def build(cls, **kwargs):
        run = kwargs.pop("run", None)
        run_id = kwargs.pop("run_id", str(uuid.uuid4()))
        seqera_run_id = kwargs.pop("seqera_run_id", None)
        submitted_at = kwargs.pop("submitted_at", None)
        binder_name = kwargs.pop("binder_name", None)
        run_name = kwargs.pop("run_name", None)
        tool = kwargs.pop("tool", "Unknown")

        if run is None:
            run = WorkflowRunFactory.build(
                seqera_run_id=seqera_run_id,
                binder_name=binder_name,
                run_name=run_name,
                submission_timestamp=submitted_at,
                submitted_form_data=None,
                work_dir=f"workdir-{run_id}",
                tool=None if tool == "Unknown" else tool,
            )
            run.metrics = None

        return UserJobListRow(
            run=run,
            run_id=run_id,
            seqera_run_id=seqera_run_id,
            workflow_type=kwargs.pop("workflow_type", "Unknown"),
            tool=tool,
            score=kwargs.pop("score", None),
            final_design_count=kwargs.pop("final_design_count", None),
            queued_status=kwargs.pop("queued_status", None),
        )
