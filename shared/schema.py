from drf_spectacular.extensions import OpenApiAuthenticationExtension


class KeycloakJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "shared.auth.KeycloakJWTAuthentication"
    name = "BearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT access token. Use: Authorization: Bearer <token>.",
        }
