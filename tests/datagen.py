import random
import uuid

from app.db.models.core import AppUser, RunInput, RunMetric, RunOutput, Workflow, WorkflowRun
from faker import Faker
from polyfactory.factories.sqlalchemy_factory import SQLAlchemyFactory

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