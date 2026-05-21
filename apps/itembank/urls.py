"""URL configuration for the itembank app.

This module defines the REST router and URL patterns for the item authoring
and asset APIs. The DefaultRouter is used to automatically generate the
standard CRUD endpoints for the registered viewsets.

Exports
-------
urlpatterns
    A list of URL patterns to be included by the project URL configuration.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    AssetViewSet,
    ItemAuthoringViewSet,
    VaultOperationsViewSet,
)

# Create a default router for viewset registration. The DefaultRouter will
# automatically create routes for list, create, retrieve, update, partial
# update and destroy actions provided by the registered viewset.
router = DefaultRouter()

# Register the viewsets under their respective prefixes. The `basename`
# argument is used to name the URL patterns (useful when the viewset does not
# provide a queryset or when custom naming is desired).
router.register(r"items", ItemAuthoringViewSet, basename="item")
router.register(r"assets", AssetViewSet, basename="asset")
router.register(r"vault", VaultOperationsViewSet, basename="vault")

# URL patterns for the itembank app. Including `router.urls` injects all
# automatically generated routes from the DefaultRouter into the project's
# URL configuration at the current path (the empty string, i.e. the root of
# this app's URL space).
urlpatterns = [
    path("", include(router.urls)),
]
