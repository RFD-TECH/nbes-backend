"""View sets for item authoring and submission workflows."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from rest_framework import viewsets, status, filters as drf_filters
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from django.core.exceptions import FieldError, ObjectDoesNotExist, ValidationError
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from shared.permissions import has_permission
from shared.exceptions import error_response, success_response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
)

from .filters import ItemFilter
from .models import Item, ItemUsage, ItemVersion, MetadataSchema, Paper, SavedSearch, VaultExportRequest
from .serializers import (
    BulkRetagSerializer,
    ItemDraftSerializer,
    ItemListSerializer,
    ItemTransitionSerializer,
    ItemVersionSerializer,
    ItemCommentSerializer,
    ManualPaperSerializer,
    MetadataSchemaSerializer,
    RuleBasedPaperSerializer,
    SavedSearchSerializer,
    SuggestionDecisionSerializer,
    VaultExportSerializer,
    PanelVoteSerializer,
)
from .tasks import dispatch_vault_export_alert
from .services import (
    bulk_retag_items,
    create_metadata_schema,
    create_or_update_item_draft,
    create_manual_paper,
    get_active_schema,
    submit_item_for_review,
    submit_paper_for_approval,
    restore_item_version,
    process_suggestion_decision,
    register_panel_vote,
    execute_vault_cosign,
    export_paper_digital,
    export_paper_docx,
    export_paper_pdf,
    generate_paper_rule_based,
    record_paper_export,
    transition_item,
)
from apps.audit.models import AuditEvent
from shared import rbac as shared_rbac
from .serializers import AssetUploadSerializer
from .services import process_asset_upload


def _validation_error_message(exc: ValidationError) -> str:
    """Render Django validation errors safely across Django versions."""

    messages = getattr(exc, "messages", None)
    if messages:
        return " ".join(str(message) for message in messages)

    message_dict = getattr(exc, "message_dict", None)
    if isinstance(message_dict, dict):
        parts = []
        for field, values in message_dict.items():
            if isinstance(values, (list, tuple, set)):
                rendered_values = ", ".join(str(value) for value in values)
            else:
                rendered_values = str(values)
            parts.append(f"{field}: {rendered_values}")
        if parts:
            return " ".join(parts)

    return str(exc)


def _item_search_queryset(base_qs):
    latest_usage = ItemUsage.objects.filter(item_id=OuterRef("pk")).order_by(
        "-recorded_at"
    )
    current_version = ItemVersion.objects.filter(
        item_id=OuterRef("pk"), id=OuterRef("current_version_id")
    )
    return base_qs.annotate(
        usage_count=Coalesce(
            Sum("usage_history__count"), Value(0), output_field=IntegerField()
        ),
        latest_facility_index=Subquery(latest_usage.values("facility_index")[:1]),
        latest_discrimination_index=Subquery(
            latest_usage.values("discrimination_index")[:1]
        ),
        current_version_content=Subquery(current_version.values("content")[:1]),
    )


def _resolve_request_user(request):
    User = get_user_model()
    try:
        return User.objects.get(keycloak_sub=request.auth["sub"])
    except (FieldError, KeyError, ObjectDoesNotExist):
        return None


@extend_schema_view(
    partial_update=extend_schema(),
    submit=extend_schema(
        request=None,
    ),
    versions=extend_schema(),
    restore=extend_schema(
        request=None,
        parameters=[
            OpenApiParameter("version_id", OpenApiTypes.INT, OpenApiParameter.PATH),
        ],
    ),
    comments=extend_schema(
        request=ItemCommentSerializer,
    ),
    decide_suggestion=extend_schema(
        request=SuggestionDecisionSerializer,
        parameters=[
            OpenApiParameter("suggestion_id", OpenApiTypes.INT, OpenApiParameter.PATH),
        ],
    ),
    votes=extend_schema(
        request=PanelVoteSerializer,
    ),
)
class ItemAuthoringViewSet(viewsets.GenericViewSet):
    """Expose draft creation, auto-save, and submission endpoints for items."""

    # Enforce Phase 1 RBAC: Only authorized Item Writers can access this
    permission_classes = [has_permission("item:create")]
    serializer_class = ItemDraftSerializer

    def create(self, request):
        """Create a new item draft."""

        # Validate the incoming payload before persisting any draft data.
        serializer = ItemDraftSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid item data",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        item = create_or_update_item_draft(serializer.validated_data, request.auth)

        return success_response(
            data={"item_id": str(item.id), "status": item.status},
            message="Item draft created successfully.",
            status_code=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, pk=None):
        """Auto-save an existing item draft."""

        # Validate partial updates so only provided fields are checked.
        serializer = ItemDraftSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return error_response(
                "Invalid item data",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            item = create_or_update_item_draft(
                serializer.validated_data, request.auth, item_id=pk
            )
            return success_response(
                data={
                    "item_id": str(item.id),
                    "current_version": str(item.current_version_id),
                },
                message="Draft auto-saved.",
            )
        except ObjectDoesNotExist:
            return error_response(
                "Item not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        except ValueError as e:
            return error_response(str(e), status_code=status.HTTP_403_FORBIDDEN)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """Submit an item draft for review."""

        # Submit only after service-layer validation and workflow checks pass.
        try:
            item = submit_item_for_review(item_id=pk, author_auth=request.auth)

            return success_response(
                data={"item_id": str(item.id), "status": item.status},
                message="Item successfully submitted for review.",
            )
        except ObjectDoesNotExist:
            return error_response(
                "Item not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        except ValidationError as e:
            # Catches the Metadata Guard failure
            return error_response(
                _validation_error_message(e),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    @action(detail=True, methods=["get"])
    def versions(self, request, pk=None):
        """Retrieve item version history or specific versions for comparison."""
        try:
            item = Item.objects.get(id=pk)
            v1_id = request.query_params.get("v1")
            v2_id = request.query_params.get("v2")

            if v1_id and v2_id:
                # Return exactly two versions for the side-by-side diff
                version_list = list(item.versions.filter(id__in=[v1_id, v2_id]))
                version_by_id = {str(version.id): version for version in version_list}
                if len(version_list) != 2:
                    return error_response(
                        "One or both specified versions were not found.",
                        status_code=404,
                    )
                v1 = version_by_id.get(v1_id)
                v2 = version_by_id.get(v2_id)
                if v1 is None or v2 is None:
                    return error_response(
                        "One or both specified versions were not found.",
                        status_code=404,
                    )
                versions = [v1, v2]
            else:
                # Return standard descending history list
                versions = item.versions.all().order_by("-version_no")

            serializer = ItemVersionSerializer(versions, many=True)
            return success_response(data=serializer.data)

        except ObjectDoesNotExist:
            return error_response("Item not found.", status_code=404)

    @action(
        detail=True,
        methods=["post"],
        url_path="versions/(?P<version_id>[^/.]+)/restore",
    )
    def restore(self, request, pk=None, version_id=None):
        """
        Non-destructive restore.
        """
        try:
            item = restore_item_version(
                item_id=pk, version_id=version_id, actor_auth=request.auth
            )
            return success_response(
                data={
                    "item_id": str(item.id),
                    "current_version_id": str(item.current_version_id),
                },
                message="Version restored successfully.",
            )
        except ObjectDoesNotExist:
            return error_response("Item not found.", status_code=404)
        except ValueError as e:
            return error_response(str(e), status_code=422)

    @action(detail=True, methods=["post"])
    def comments(self, request, pk=None):
        """
        Annotate specific portions of an item.
        """
        # Assign the currently authenticated user as the creator
        serializer = ItemCommentSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid comment data", errors=serializer.errors, status_code=400
            )

        try:
            # Verify the item actually exists
            item = Item.objects.get(id=pk)
            if not item.current_version_id:
                return error_response(
                    "Item has no active version.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            current_version = item.versions.get(id=item.current_version_id)

            comment = serializer.save(
                created_by_id=request.auth["sub"],
                item_version_id=current_version,
            )
            return success_response(
                data=ItemCommentSerializer(comment).data,
                message="Annotation added successfully.",
                status_code=201,
            )
        except ObjectDoesNotExist:
            return error_response("Item not found.", status_code=404)

    @action(
        detail=True,
        methods=["post"],
        url_path="suggestions/(?P<suggestion_id>[^/.]+)/decide",
    )
    def decide_suggestion(self, request, pk=None, suggestion_id=None):
        """
        Accept or decline a suggestion with rationale.
        """
        serializer = SuggestionDecisionSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid decision data", errors=serializer.errors, status_code=400
            )

        try:
            result = process_suggestion_decision(
                item_id=pk,
                suggestion_id=suggestion_id,
                data=serializer.validated_data,
                actor_auth=request.auth,
            )
            decision = serializer.validated_data["decision"]
            past_tense = {"accept": "accepted", "decline": "declined"}.get(
                decision, f"{decision}ed"
            )
            return success_response(
                data=result,
                message=f"Suggestion {past_tense}.",
            )
        except ObjectDoesNotExist:
            return error_response("Item not found.", status_code=404)
        except ValueError as e:
            return error_response(str(e), status_code=422)

    @action(detail=True, methods=["post"])
    def votes(self, request, pk=None):
        """Record a panel vote (accept or decline) for an item.
        Expects a payload with 'vote' (accept/decline) and optional 'justification'.
        """
        vote_type = request.data.get("vote")
        justification = request.data.get("justification")
        panellist_id = request.auth["sub"]

        try:
            item = register_panel_vote(pk, panellist_id, vote_type, justification)
            return success_response(
                data={"item_id": str(item.id), "status": item.status},
                message="Panel verdict recorded cleanly.",
            )
        except ValidationError as e:
            return error_response(
                _validation_error_message(e),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError as e:
            return error_response(
                str(e), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Generic workflow transition (Submitted→In Review, In Review→Reviewed, Reviewed→Revised).

        Implements SRS-NBE-F02-04 peer-review workflow transitions. Role and
        ownership checks are enforced by the service layer; the view simply
        validates the request shape and maps exceptions to HTTP responses.
        """
        serializer = ItemTransitionSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid transition data",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            item = transition_item(
                item_id=pk,
                target_state=serializer.validated_data["target_state"],
                actor_auth=request.auth,
                reviewer_id=serializer.validated_data.get("reviewer_id"),
                notes=serializer.validated_data.get("notes"),
            )
            return success_response(
                data={"item_id": str(item.id), "status": item.status},
                message="Workflow transition applied.",
            )
        except ObjectDoesNotExist:
            return error_response("Item or user not found.", status_code=status.HTTP_404_NOT_FOUND)
        except ValueError as e:
            return error_response(str(e), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)


class AssetViewSet(viewsets.GenericViewSet):
    """Expose asset upload endpoint for item authoring workflows."""

    # Only Item Writers can upload assets
    permission_classes = [has_permission("item:create")]
    serializer_class = AssetUploadSerializer

    # Must use MultiPartParser to accept physical files over HTTP
    parser_classes = [MultiPartParser]

    def create(self, request):
        """Upload a new asset for item authoring."""
        serializer = AssetUploadSerializer(data=request.data)

        if not serializer.is_valid():
            return error_response(
                "Invalid file upload",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            file_obj = serializer.validated_data["file"]
            asset_ref = process_asset_upload(file_obj)

            # Auto-Save PATCH request
            return success_response(
                data={"asset_ref": asset_ref},
                message="Asset scanned and stored successfully.",
                status_code=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            # Catches the Virus Scan failure
            return error_response(
                str(e), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )


@extend_schema_view(
    cosign_export=extend_schema(
        request=None,
    )
)
class VaultOperationsViewSet(viewsets.GenericViewSet):
    """Expose vault export request and cosign operations."""

    permission_classes = [has_permission("vault:operate")]
    serializer_class = VaultExportSerializer

    @action(detail=False, methods=["post"], url_path="export-requests")
    def initiate_export(self, request):
        """Create a new vault export request.

        Stores the requested scope and purpose, assigns the authenticated
        requester, and sets the request to expire in 72 hours.
        """

        serializer = VaultExportSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid data",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        User = get_user_model()
        try:
            requester = User.objects.get(keycloak_sub=request.auth["sub"])
        except (ObjectDoesNotExist, FieldError, KeyError):
            return error_response(
                "Requester not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        req = VaultExportRequest.objects.create(
            scope=serializer.validated_data["scope"],
            purpose=serializer.validated_data["purpose"],
            requester_id=requester,
            status="Pending",
            expires_at=timezone.now() + timedelta(hours=72),
        )

        # Immediately alert Chair/DG after the row commits (SRS-NBE-F02-07 / NBE-N01).
        transaction.on_commit(
            lambda req_id=str(req.id), sub=str(request.auth["sub"]), sc=req.scope: (
                dispatch_vault_export_alert.delay(req_id, sub, sc)
            )
        )

        return success_response(
            data={
                "request_id": str(req.id),
                "status": req.status,
                "expires_at": req.expires_at,
            },
            message="Export request logged. Dual-control confirmation pending.",
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="cosign")
    def cosign_export(self, request, pk=None):
        """Approve an existing vault export request with a cosign.

        Verifies the second approver and returns the updated request status
        once dual-control authorization has been completed.
        """
        try:
            req = execute_vault_cosign(pk, cosigner_id=request.auth["sub"])
            return success_response(
                data={"request_id": str(req.id), "status": req.status},
                message="Dual-control authorization verified. Vault export sequence unlocked.",
            )
        except ObjectDoesNotExist:
            return error_response(
                "Export request not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        except ValueError as e:
            return error_response(
                str(e), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )


@extend_schema_view(
    create=extend_schema(request=ManualPaperSerializer),
    generate_rule_based=extend_schema(request=RuleBasedPaperSerializer),
    submit_for_approval=extend_schema(request=None),
    export_pdf=extend_schema(request=None),
    export_word=extend_schema(request=None),
    export_digital=extend_schema(request=None),
)
class PaperViewSet(viewsets.GenericViewSet):
    """Construct, generate, and export examination papers (NBE-F02-08/09)."""

    permission_classes = [has_permission("paper:construct")]
    serializer_class = ManualPaperSerializer

    def _resolve_user(self, request):
        User = get_user_model()
        try:
            return User.objects.get(keycloak_sub=request.auth["sub"])
        except (FieldError, KeyError) as exc:
            raise ObjectDoesNotExist("Requester not found.") from exc

    def create(self, request):
        """Manually construct a paper from a curated list of locked items."""
        serializer = ManualPaperSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid paper data",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            user = self._resolve_user(request)
        except ObjectDoesNotExist:
            return error_response(
                "Requester not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        try:
            paper = create_manual_paper(
                serializer.validated_data, user, request=request
            )
        except ValueError as exc:
            return error_response(
                str(exc), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        return success_response(
            data={
                "paper_id": str(paper.id),
                "status": paper.status,
                "item_ids": [str(i) for i in paper.item_ids],
                "sections": paper.sections,
                "variants": paper.variants,
            },
            message="Paper constructed successfully.",
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="generate")
    def generate_rule_based(self, request):
        """Rule-based paper generation."""
        serializer = RuleBasedPaperSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid generation parameters",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            user = self._resolve_user(request)
        except ObjectDoesNotExist:
            return error_response(
                "Requester not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        try:
            paper = generate_paper_rule_based(
                serializer.validated_data, user, request=request
            )
            return success_response(
                data={
                    "paper_id": str(paper.id),
                    "item_ids": [str(i) for i in paper.item_ids],
                    "variants": paper.variants,
                },
                message="Paper generated successfully.",
                status_code=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return error_response(
                str(e), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    def submit_for_approval(self, request, pk=None):
        """Move a constructed paper into the NBEC approval queue (F02-08)."""
        try:
            user = self._resolve_user(request)
        except ObjectDoesNotExist:
            return error_response(
                "Requester not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        try:
            paper = submit_paper_for_approval(pk, user)
        except ObjectDoesNotExist:
            return error_response(
                "Paper not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        except ValueError as exc:
            return error_response(
                str(exc), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        return success_response(
            data={"paper_id": str(paper.id), "status": paper.status},
            message="Paper submitted for NBEC approval.",
        )

    def _get_paper_or_404(self, pk):
        try:
            return Paper.objects.get(id=pk)
        except (ObjectDoesNotExist, ValueError):
            return None

    @action(detail=True, methods=["get"], url_path="export/pdf")
    def export_pdf(self, request, pk=None):
        paper = self._get_paper_or_404(pk)
        if paper is None:
            return error_response(
                "Paper not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        try:
            user = self._resolve_user(request)
        except ObjectDoesNotExist:
            return error_response(
                "Requester not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        pdf_bytes = export_paper_pdf(paper)
        record_paper_export(paper, user=user, fmt="pdf", request=request)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="paper-{paper.id}.pdf"'
        return response

    @action(detail=True, methods=["get"], url_path="export/word")
    def export_word(self, request, pk=None):
        paper = self._get_paper_or_404(pk)
        if paper is None:
            return error_response(
                "Paper not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        try:
            user = self._resolve_user(request)
        except ObjectDoesNotExist:
            return error_response(
                "Requester not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        try:
            docx_bytes = export_paper_docx(paper)
        except RuntimeError as exc:
            return error_response(str(exc), status_code=status.HTTP_501_NOT_IMPLEMENTED)
        record_paper_export(paper, user=user, fmt="docx", request=request)
        response = HttpResponse(
            docx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        response["Content-Disposition"] = (
            f'attachment; filename="paper-{paper.id}.docx"'
        )
        return response

    @action(detail=True, methods=["get"], url_path="export/digital")
    def export_digital(self, request, pk=None):
        paper = self._get_paper_or_404(pk)
        if paper is None:
            return error_response(
                "Paper not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        try:
            user = self._resolve_user(request)
        except ObjectDoesNotExist:
            return error_response(
                "Requester not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        payload = export_paper_digital(paper)
        record_paper_export(paper, user=user, fmt="digital", request=request)
        return success_response(data=payload, message="Digital paper export ready.")


def _rbac_scoped_item_queryset(request):
    """Return an ``Item`` queryset filtered to what the caller may read.

    Implements SRS-NBE-F02-10's "search is RBAC-aware" clause. Roles are
    resolved through ``shared.rbac.get_nbes_role_names`` so the resolution
    honours the IAM client-role contract (and falls back to legacy
    realm-roles when needed).

    For an Item Writer the queryset is narrowed to items they authored.
    When the local user record cannot be resolved (e.g. JWT ``sub`` not
    yet mirrored to a profile) the scope degrades to *deny by default*
    so the search never leaks items.
    """
    qs = Item.objects.all()
    payload = request.auth or {}
    role_names = set(shared_rbac.get_nbes_role_names(payload))
    if "item_writer" in role_names:
        try:
            user = get_user_model().objects.get(keycloak_sub=payload.get("sub"))
        except (ObjectDoesNotExist, FieldError):
            return qs.none()
        return qs.filter(author_id=user)
    if "moderator" in role_names or "reviewer" in role_names:
        return qs.filter(status=Item.Status.IN_REVIEW)
    return qs


class ItemSearchViewSet(viewsets.ReadOnlyModelViewSet):
    """Advanced item search (SRS-NBE-F02-10).

    RBAC-scoped via ``shared.rbac``. Item Writers see only items they
    authored; Moderators/Reviewers see items in ``In Review``; everyone
    else with ``item:search`` may search the whole bank.
    """

    permission_classes = [has_permission("item:search")]
    serializer_class = ItemListSerializer
    filter_backends = [
        DjangoFilterBackend,
        drf_filters.SearchFilter,
        drf_filters.OrderingFilter,
    ]
    filterset_class = ItemFilter
    # SRS-NBE-F02-10 keyword search must hit stem, options, rubric, metadata.
    # Item content lives on the *current* version only, not all historical
    # versions; we still expose subject/topic/blueprint_ref for metadata matching.
    search_fields = ["subject", "topic", "blueprint_ref", "current_version_content"]
    ordering_fields = ["subject", "topic", "difficulty", "marks", "updated_at"]
    ordering = ["subject", "topic"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Item.objects.none()
        return _item_search_queryset(_rbac_scoped_item_queryset(self.request))

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        AuditEvent.record(
            actor_id=(request.auth or {}).get("sub"),
            action="SEARCH_EXECUTED",
            entity_type="item",
            new_state={
                "filters": dict(getattr(request, "query_params", None) or request.GET),
                "result_count": (
                    response.data.get("count")
                    if isinstance(response.data, dict)
                    else None
                ),
            },
            ip_address=getattr(request, "ip_address", None),
        )
        return response

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Export the filtered search results (SRS-NBE-F02-10).

        Returns a JSON payload of the filter result and writes a
        ``SEARCH_EXPORTED`` audit row capturing the filters and the
        resulting count, per the SRS.
        """
        queryset = self.filter_queryset(self.get_queryset())
        # Hard cap on exports so a single request can't dump the bank.
        rows = ItemListSerializer(queryset[:5000], many=True).data
        AuditEvent.record(
            actor_id=(request.auth or {}).get("sub"),
            action="SEARCH_EXPORTED",
            entity_type="item",
            new_state={
                "filters": dict(getattr(request, "query_params", None) or request.GET),
                "result_count": len(rows),
            },
            ip_address=getattr(request, "ip_address", None),
        )
        return success_response(
            data={"count": len(rows), "results": rows},
            message="Search results exported.",
        )


@extend_schema_view(
    results=extend_schema(request=None),
)
class SavedSearchViewSet(viewsets.ModelViewSet):
    """CRUD for per-user saved searches (SRS-NBE-F02-10)."""

    permission_classes = [has_permission("search:manage")]
    serializer_class = SavedSearchSerializer
    queryset = SavedSearch.objects.none()

    def _viewer_is_secretariat(self) -> bool:
        return "nbec_secretariat" in set(
            shared_rbac.get_nbes_role_names(self.request.auth or {})
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SavedSearch.objects.none()
        user = _resolve_request_user(self.request)
        # Secretariat can still see *shared* searches even when their own
        # auth.User row can't be resolved (auth.User has no keycloak_sub
        # column in the current schema).
        if user is None:
            if self._viewer_is_secretariat():
                return SavedSearch.objects.filter(
                    shared_with_secretariat=True
                ).order_by("-updated_at")
            return SavedSearch.objects.none()
        from django.db.models import Q as _Q

        qs = SavedSearch.objects.filter(user=user)
        if self._viewer_is_secretariat():
            qs = SavedSearch.objects.filter(
                _Q(user=user) | _Q(shared_with_secretariat=True)
            )
        return qs.order_by("-updated_at")

    def perform_create(self, serializer):
        user = _resolve_request_user(self.request)
        if user is None:
            raise NotFound("Requester not found.")
        serializer.save(user=user)
        AuditEvent.record(
            actor_id=self.request.auth["sub"],
            action="SAVED_SEARCH_CREATED",
            entity_type="saved_search",
            entity_id=serializer.instance.id,
            new_state={
                "name": serializer.instance.name,
                "shared": serializer.instance.shared_with_secretariat,
            },
        )

    def perform_update(self, serializer):
        super().perform_update(serializer)
        AuditEvent.record(
            actor_id=self.request.auth["sub"],
            action="SAVED_SEARCH_UPDATED",
            entity_type="saved_search",
            entity_id=serializer.instance.id,
            new_state={
                "name": serializer.instance.name,
                "shared": serializer.instance.shared_with_secretariat,
            },
        )

    def perform_destroy(self, instance):
        AuditEvent.record(
            actor_id=self.request.auth["sub"],
            action="SAVED_SEARCH_DELETED",
            entity_type="saved_search",
            entity_id=instance.id,
            old_state={"name": instance.name},
        )
        super().perform_destroy(instance)

    @action(detail=True, methods=["get"], url_path="results")
    def results(self, request, pk=None):
        """Execute the stored query under the caller's RBAC scope."""
        try:
            saved = self.get_queryset().get(id=pk)
        except (ObjectDoesNotExist, ValueError, ValidationError):
            return error_response(
                "Saved search not found.", status_code=status.HTTP_404_NOT_FOUND
            )

        query = saved.query if isinstance(saved.query, dict) else {}
        # Apply the same RBAC scope the live search endpoint uses so a
        # shared saved search NEVER leaks items the viewer cannot see.
        base_qs = _item_search_queryset(_rbac_scoped_item_queryset(request))
        filter_params = {
            key: value
            for key, value in query.items()
            if key not in {"search", "ordering"}
        }
        filterset = ItemFilter(filter_params, queryset=base_qs)
        if not filterset.is_valid():
            return error_response(
                "Saved search contains invalid filter parameters.",
                errors=filterset.errors,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        factory = APIRequestFactory()
        raw_request = factory.get(
            getattr(request, "path", "/api/v1/itembank/item-search/"),
            data=query,
        )
        search_request = Request(raw_request)
        setattr(search_request, "_auth", request.auth)
        setattr(search_request, "_user", getattr(request, "user", None))
        search_viewset = ItemSearchViewSet()
        setattr(search_viewset, "request", search_request)
        setattr(search_viewset, "kwargs", {})
        setattr(search_viewset, "action", "list")
        setattr(search_viewset, "format_kwarg", None)
        items = search_viewset.filter_queryset(base_qs)[:200]
        data = ItemListSerializer(items, many=True).data
        AuditEvent.record(
            actor_id=(request.auth or {}).get("sub"),
            action="SAVED_SEARCH_EXECUTED",
            entity_type="saved_search",
            entity_id=saved.id,
            new_state={"result_count": len(data)},
            ip_address=getattr(request, "ip_address", None),
        )
        return success_response(
            data={"count": len(data), "results": data},
            message="Saved search executed.",
        )


class MetadataSchemaViewSet(viewsets.ModelViewSet):
    """Admin-only: manage versioned metadata schemas (SRS-NBE-F02-02).

    Provides full CRUD for MetadataSchema records. Schema versions are
    auto-incremented by the service layer; only one schema may be active at
    a time. Bulk re-tagging of items requires Administrator approval and
    produces a per-item audit entry.
    """

    permission_classes = [has_permission("schema:manage")]
    serializer_class = MetadataSchemaSerializer
    queryset = MetadataSchema.objects.all().order_by("-version")

    def create(self, request, *args, **kwargs):
        """Create a new versioned metadata schema."""
        serializer = MetadataSchemaSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid schema data",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            schema = create_metadata_schema(
                serializer.validated_data, admin_auth=request.auth
            )
            return success_response(
                data=MetadataSchemaSerializer(schema).data,
                message="Metadata schema created.",
                status_code=status.HTTP_201_CREATED,
            )
        except ObjectDoesNotExist:
            return error_response(
                "Admin user not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        except ValueError as e:
            return error_response(str(e), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        """Deactivate all other schemas and activate this one."""
        try:
            schema = MetadataSchema.objects.get(id=pk)
        except ObjectDoesNotExist:
            return error_response("Schema not found.", status_code=status.HTTP_404_NOT_FOUND)

        MetadataSchema.objects.filter(is_active=True).exclude(id=pk).update(is_active=False)
        schema.is_active = True
        schema.save(update_fields=["is_active"])

        AuditEvent.record(
            actor_id=(request.auth or {}).get("sub"),
            action="METADATA_SCHEMA_ACTIVATED",
            entity_type="metadata_schema",
            entity_id=str(schema.id),
            new_state={"version": schema.version, "is_active": True},
        )
        return success_response(
            data=MetadataSchemaSerializer(schema).data,
            message=f"Metadata schema v{schema.version} is now active.",
        )

    @action(detail=False, methods=["post"], url_path="bulk-retag")
    def bulk_retag(self, request):
        """Bulk re-tag items with Administrator approval (SRS-NBE-F02-02)."""
        serializer = BulkRetagSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid bulk retag data",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = bulk_retag_items(
                item_ids=serializer.validated_data["item_ids"],
                updates=serializer.validated_data["updates"],
                admin_auth=request.auth,
            )
            return success_response(
                data=result,
                message=f"{result['retagged_count']} item(s) re-tagged successfully.",
            )
        except ObjectDoesNotExist:
            return error_response(
                "Admin user not found.", status_code=status.HTTP_404_NOT_FOUND
            )
        except ValueError as e:
            return error_response(str(e), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @action(detail=False, methods=["get"], url_path="active")
    def active_schema(self, request):
        """Return the currently active metadata schema."""
        schema = get_active_schema()
        if schema is None:
            return success_response(
                data=None,
                message="No active metadata schema configured.",
            )
        return success_response(
            data=MetadataSchemaSerializer(schema).data,
            message="Active metadata schema retrieved.",
        )
