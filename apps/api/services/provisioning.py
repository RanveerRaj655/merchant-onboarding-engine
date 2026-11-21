from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.db.models import Application, MerchantAccount, ReviewCase
from apps.api.services.state_machine import StateMachineService
from shared.enums import (
    ApplicationStatus,
    ProvisioningStatus,
    ReviewCaseStatus,
    ReviewReason,
)


class ProvisioningService:
    def __init__(self, db: Session):
        self.db = db
        self.sm = StateMachineService(db)

    def provision(self, application_id: UUID, *, force_fail: bool = False) -> None:
        app = self.db.get(Application, application_id)
        if app is None:
            return

        if app.status == ApplicationStatus.CHECKS_PASSED.value:
            self.sm.transition(
                application_id,
                ApplicationStatus.PROVISIONING,
                actor="system",
                action="START_PROVISION",
            )

        if app.status != ApplicationStatus.PROVISIONING.value:
            return
        from apps.api.services.verification import VerificationOrchestrator
        if not VerificationOrchestrator(self.db).checks_passed(app):
            raise ValueError("Required checks must pass for current details before provisioning")

        scenario = (app.config_checks or {}).get("scenario")
        should_fail = force_fail or scenario == "provision_fail"

        existing = (
            self.db.query(MerchantAccount)
            .filter(MerchantAccount.application_id == application_id)
            .first()
        )

        if should_fail and (existing is None or not existing.fail_once):
            # Fail once then allow retry to succeed
            if existing is None:
                self.db.add(
                    MerchantAccount(
                        application_id=application_id,
                        provisioning_status=ProvisioningStatus.FAILED.value,
                        fail_once=True,
                        capabilities={},
                    )
                )
            else:
                existing.provisioning_status = ProvisioningStatus.FAILED.value
                existing.fail_once = True

            self.sm.transition(
                application_id,
                ApplicationStatus.IN_REVIEW,
                actor="system",
                action="PROVISION_FAIL",
                payload={"reason": ReviewReason.PROVISION_FAIL.value},
            )
            self.db.add(
                ReviewCase(
                    application_id=application_id,
                    reason_codes=[ReviewReason.PROVISION_FAIL.value],
                    status=ReviewCaseStatus.OPEN.value,
                    priority=90,
                )
            )
            self.db.commit()
            return

        if existing and existing.provisioning_status == ProvisioningStatus.ACTIVE.value:
            if app.status != ApplicationStatus.APPROVED.value:
                self.sm.transition(
                    application_id,
                    ApplicationStatus.APPROVED,
                    actor="system",
                    action="MERCHANT_ALREADY_ACTIVE",
                    payload={"merchant_id": str(existing.id)},
                )
            self.db.commit()
            return

        if existing is None:
            merchant = MerchantAccount(
                application_id=application_id,
                capabilities={"card": True, "upi": True, "netbanking": False},
                provisioning_status=ProvisioningStatus.ACTIVE.value,
                fail_once=False,
            )
            self.db.add(merchant)
            self.db.flush()
        else:
            existing.provisioning_status = ProvisioningStatus.ACTIVE.value
            existing.capabilities = {"card": True, "upi": True, "netbanking": False}
            merchant = existing

        self.sm.transition(
            application_id,
            ApplicationStatus.APPROVED,
            actor="system",
            action="MERCHANT_CREATED",
            payload={
                "merchant_id": str(merchant.id),
                "capabilities": merchant.capabilities,
            },
        )
        # Resolve open review cases
        for case in (
            self.db.query(ReviewCase)
            .filter(
                ReviewCase.application_id == application_id,
                ReviewCase.status != ReviewCaseStatus.RESOLVED.value,
            )
            .all()
        ):
            case.status = ReviewCaseStatus.RESOLVED.value
            case.resolution = "APPROVED"

        self.db.commit()
