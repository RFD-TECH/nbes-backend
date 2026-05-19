"""apps/committee/urls.py — NBEC Committee URL patterns.

Mounted at /api/v1/nbec/ in config/urls.py.
"""
from django.urls import path

from .views import (
    COICreateView,
    COIPolicyView,
    COIReviewView,
    MeetingAdjournView,
    MeetingAgendaView,
    MeetingAttendanceView,
    MeetingConveneView,
    MeetingCreateView,
    MemberActivateView,
    MemberDetailView,
    MemberListCreateView,
    MinutesAddendumView,
    MinutesSignView,
)

urlpatterns = [
    # Members
    path("members/", MemberListCreateView.as_view(), name="nbec-member-create"),
    path("members/<uuid:pk>/", MemberDetailView.as_view(), name="nbec-member-detail"),
    path("members/<uuid:pk>/activate/", MemberActivateView.as_view(), name="nbec-member-activate"),

    # Conflict of Interest
    path("coi/", COICreateView.as_view(), name="nbec-coi-create"),
    path("coi/<uuid:pk>/review/", COIReviewView.as_view(), name="nbec-coi-review"),

    # Meetings
    path("meetings/", MeetingCreateView.as_view(), name="nbec-meeting-create"),
    path("meetings/<uuid:pk>/agenda/", MeetingAgendaView.as_view(), name="nbec-meeting-agenda"),
    path("meetings/<uuid:pk>/attendance/", MeetingAttendanceView.as_view(), name="nbec-meeting-attendance"),
    path("meetings/<uuid:pk>/convene/", MeetingConveneView.as_view(), name="nbec-meeting-convene"),
    path("meetings/<uuid:pk>/adjourn/", MeetingAdjournView.as_view(), name="nbec-meeting-adjourn"),

    # Minutes
    path("minutes/<uuid:pk>/sign/", MinutesSignView.as_view(), name="nbec-minutes-sign"),
    path("minutes/<uuid:pk>/addendum/", MinutesAddendumView.as_view(), name="nbec-minutes-addendum"),

    # Internal policy endpoints
    path("policy/coi/", COIPolicyView.as_view(), name="nbec-policy-coi"),
]
