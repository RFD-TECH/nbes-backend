"""View sets for item authoring and submission workflows."""

from datetime import timedelta

from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.utils import timezone
from shared.permissions import has_permission
from shared.exceptions import error_response, success_response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
)

from .models import Item, VaultExportRequest
from .serializers import (
    ItemDraftSerializer,
    ItemVersionSerializer,
    ItemCommentSerializer,
    SuggestionDecisionSerializer,
    VaultExportSerializer,
    PanelVoteSerializer,
)
from .services import (
    create_or_update_item_draft,
    submit_item_for_review,
    restore_item_version,
    process_suggestion_decision,
    register_panel_vote,
    execute_vault_cosign,
)
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


@extend_schema_view(
    partial_update=extend_schema(
    ),
    submit=extend_schema(
        request=None,
    ),
    versions=extend_schema(
    ),
    restore=extend_schema(
        request=None,
        parameters=[
            OpenApiParameter("version_id", OpenApiTypes.INT, OpenApiParameter.PATH),
        ]
    ),
    comments=extend_schema(
        request=ItemCommentSerializer,
    ),
    decide_suggestion=extend_schema(
        request=SuggestionDecisionSerializer,
        parameters=[
            OpenApiParameter("suggestion_id", OpenApiTypes.INT, OpenApiParameter.PATH),
        ]
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
                    "Item has no active version.", status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
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

        req = VaultExportRequest.objects.create(
            scope=serializer.validated_data["scope"],
            purpose=serializer.validated_data["purpose"],
            requester_id=request.auth["sub"],
            expires_at=timezone.now() + timedelta(hours=72),
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
