"""Native S3 and managed Globus movement shared by workflow preparation and results."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy.orm import Session

from ..config import GlobusSettings, Settings, get_settings
from ..db.models import WorkflowRun
from ..db.models.core import DataTransfer, RunInput, S3Object
from ..schemas.backend import DataTransferMode
from .compute_backends import get_run_globus_settings
from .datasets import upload_csv_to_s3, upload_samplesheet_rows_to_s3
from .globus_errors import GlobusConfigurationError
from .globus_transfer import build_gadi_input_path, build_gadi_output_path
from .s3 import s3_uri_to_key


def uses_native_data(run: WorkflowRun) -> bool:
    return run.backend is not None and run.backend.data_transfer == DataTransferMode.NATIVE


@dataclass
class DataMovement:
    """
    Handles input/output data movement for a run,
    based on whether data transfer occurs natively or through
    globus.
    """

    mode: DataTransferMode
    settings: Settings
    globus: GlobusSettings | None = None

    def __post_init__(self) -> None:
        if self.mode == DataTransferMode.GLOBUS:
            self.require_globus()

    @classmethod
    def for_run(cls, run: WorkflowRun, settings: Settings | None = None) -> DataMovement:
        settings = settings or get_settings()
        if uses_native_data(run):
            return cls(DataTransferMode.NATIVE, settings)
        return cls(DataTransferMode.GLOBUS, settings, get_run_globus_settings(run, settings.globus))

    def require_globus(self) -> GlobusSettings:
        if self.globus is None:
            raise GlobusConfigurationError("Globus settings are required for managed data movement")
        return self.globus

    def s3_uri(self, key: str) -> str:
        if key.startswith("s3://"):
            return key
        return f"s3://{self.settings.aws.s3_bucket}/{key.lstrip('/')}"

    def output_location(self, run_id: object, workflow_name: str) -> str:
        if self.mode == DataTransferMode.NATIVE:
            return self.s3_uri(str(run_id))
        return build_gadi_output_path(run_id, workflow_name, globus_settings=self.require_globus())

    def prepare_input(
        self,
        session: Session,
        run_id: UUID,
        workflow_name: str,
        source: str,
    ) -> str:
        """
        Ensure inputs are recorded in the DB, creating entries/data transfers for them if
        needed. If the inputs already exist, just return their location.

        Native inputs stay in S3, Globus inputs create a pending DataTransfer
        """
        source = source.strip()
        key = s3_uri_to_key(source)
        if not key:
            raise ValueError("Invalid S3 input location")
        source_uri = self.s3_uri(source)
        obj = self.get_or_create_input_source(session, key, source_uri)
        existing = session.get(RunInput, (run_id, key))
        if existing is not None:
            if self.mode == DataTransferMode.GLOBUS and existing.data_transfer is not None:
                return existing.data_transfer.destination_location
            return source_uri
        if self.mode == DataTransferMode.NATIVE:
            return self.prepare_native_input(session, run_id, obj, source_uri)
        return self.prepare_globus_input(session, run_id, workflow_name, obj, source_uri)

    def get_or_create_input_source(self, session: Session, key: str, source_uri: str) -> S3Object:
        """Register the S3 object, flushing previous inputs so duplicate references reuse it."""
        session.flush()
        obj = session.get(S3Object, key)
        if obj is None:
            obj = S3Object(object_key=key, uri=source_uri)
            session.add(obj)
        return obj

    def prepare_native_input(
        self, session: Session, run_id: UUID, obj: S3Object, source_uri: str
    ) -> str:
        """Record a new input without a transfer and return its S3 URI."""
        session.add(RunInput(run_id=run_id, s3_object=obj))
        session.flush()
        return source_uri

    def prepare_globus_input(
        self, session: Session, run_id: UUID, workflow_name: str, obj: S3Object, source_uri: str
    ) -> str:
        """Record a new input and pending Globus transfer, returning its backend path."""
        location = build_gadi_input_path(
            run_id,
            workflow_name,
            PurePosixPath(obj.object_key).name,
            globus_settings=self.require_globus(),
        )
        transfer = DataTransfer(
            workflow_run_id=run_id,
            direction="input",
            provider="globus",
            source_location=source_uri,
            destination_location=location,
            recursive=False,
        )
        session.add(RunInput(run_id=run_id, s3_object=obj, data_transfer=transfer))
        session.flush()
        return location

    async def prepare_samplesheet(
        self,
        session: Session,
        run_id: UUID,
        workflow_name: str,
        source_key: str,
        rows: list[dict[str, str]],
        reference_field: str,
        *,
        single_row: bool = False,
    ) -> str:
        prepared_rows = []
        for row in rows:
            prepared = dict(row)
            prepared[reference_field] = self.prepare_input(
                session, run_id, workflow_name, row[reference_field].strip()
            )
            prepared_rows.append(prepared)
        if self.mode == DataTransferMode.NATIVE:
            return source_key
        result = (
            await upload_csv_to_s3(prepared_rows[0])
            if single_row
            else await upload_samplesheet_rows_to_s3(prepared_rows)
        )
        return result.file_key
