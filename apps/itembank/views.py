"""View sets for item authoring and submission workflows."""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from django.core.exceptions import ObjectDoesNotExist, ValidationError

from shared.permissions import has_permission
from shared.exceptions import error_response, success_response

from .serializers import ItemDraftSerializer
from .services import create_or_update_item_draft, submit_item_for_review
from .serializers import AssetUploadSerializer
from .services import process_asset_upload


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
                str(e.message), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @action(detail=True, methods=["post"])
    def comments(self, _request, _pk=None):
        """Scaffold for Sprint 3.2: Annotate specific portions of an item."""
        return Response(
            {"status": "pending_sprint_3.2"},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )

    @action(detail=True, methods=["post"])
    def votes(self, _request, _pk=None):
        """Scaffold for Sprint 3.3: Record a panel vote."""
        return Response(
            {"status": "pending_sprint_3.3"},
            status=status.HTTP_501_NOT_IMPLEMENTED,
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


class VaultOperationsViewSet(viewsets.GenericViewSet):
    """Scaffold for Sprint 3.3 vault operations."""

    @action(detail=False, methods=["post"], url_path="export-requests")
    def create_export_request(self, _request):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)

    @action(detail=True, methods=["post"])
    def cosign(self, _request, _pk=None):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)


class PaperConstructionViewSet(viewsets.GenericViewSet):
    """Scaffold for Sprint 3.4 paper construction."""

    def create(self, _request):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)

    @action(detail=False, methods=["post"])
    def generate(self, _request):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)
