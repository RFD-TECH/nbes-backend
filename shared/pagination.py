"""
shared/pagination.py — Standard Pagination for NBES API
========================================================

All paginated responses include:
    {
        "success": true,
        "data": [ ... ],
        "meta": {
            "page": 1,
            "total": 42,
            "pages": 3,
            "request_id": "uuid"
        }
    }
"""

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardResultsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200

    def get_paginated_response(self, data):
        return Response({
            "success": True,
            "data": data,
            "meta": {
                "page": self.page.number,
                "total": self.page.paginator.count,
                "pages": self.page.paginator.num_pages,
            },
        })

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "data": schema,
                "meta": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer"},
                        "total": {"type": "integer"},
                        "pages": {"type": "integer"},
                    },
                },
            },
        }
