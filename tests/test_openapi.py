"""Tests for the published API contract.

A specification that drifts from the implementation is worse than none: it
tells integrators something confidently false. These tests fail if a route is
added without documenting it, or documented without existing.
"""

from __future__ import annotations

import json
import re
import unittest

from starlette.routing import Mount, Route

import openapi
import service

# Paths served for the browser rather than as API surface.
UI_PATHS = {"/static", "/docs", "/openapi.json"}


def _implemented_operations() -> set[tuple[str, str]]:
    """(path, method) pairs the service actually serves."""

    operations: set[tuple[str, str]] = set()
    for route in service.app.routes:
        if isinstance(route, Mount) or not isinstance(route, Route):
            continue
        if route.path in UI_PATHS:
            continue
        for method in route.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            operations.add((route.path, method.lower()))
    return operations


def _documented_operations() -> set[tuple[str, str]]:
    spec = openapi.build_spec()
    return {
        (path, method)
        for path, item in spec["paths"].items()
        if path not in UI_PATHS
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }


class SpecificationDriftTests(unittest.TestCase):
    def test_every_route_is_documented(self):
        missing = _implemented_operations() - _documented_operations()
        self.assertFalse(
            missing,
            "These endpoints exist but are absent from openapi.py: "
            + ", ".join(f"{method.upper()} {path}" for path, method in sorted(missing)),
        )

    def test_every_documented_operation_exists(self):
        phantom = _documented_operations() - _implemented_operations()
        self.assertFalse(
            phantom,
            "These endpoints are documented but not served: "
            + ", ".join(f"{method.upper()} {path}" for path, method in sorted(phantom)),
        )

    def test_path_parameters_match_the_route_placeholders(self):
        spec = openapi.build_spec()
        for path, item in spec["paths"].items():
            placeholders = set(re.findall(r"\{(\w+)\}", path))
            for method, operation in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                declared = {
                    parameter["name"]
                    for parameter in operation.get("parameters", [])
                    if parameter.get("in") == "path"
                }
                self.assertEqual(
                    placeholders,
                    declared,
                    f"{method.upper()} {path}: path parameters must all be declared",
                )


class SpecificationShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = openapi.build_spec("https://fleet.example.run.app")

    def test_it_is_serialisable_and_versioned(self):
        encoded = json.dumps(self.spec)
        self.assertGreater(len(encoded), 5000)
        self.assertEqual(self.spec["openapi"], "3.1.0")
        self.assertTrue(self.spec["info"]["version"])

    def test_server_url_is_pinned_to_the_deployment(self):
        self.assertEqual(self.spec["servers"][0]["url"], "https://fleet.example.run.app")
        # With no origin known, relative is correct rather than a guessed host.
        self.assertEqual(openapi.build_spec()["servers"][0]["url"], "/")

    def test_every_reference_resolves(self):
        schemas = self.spec["components"]["schemas"]
        encoded = json.dumps(self.spec)
        for ref in set(re.findall(r'"#/components/schemas/(\w+)"', encoded)):
            self.assertIn(ref, schemas, f"dangling $ref to {ref}")

    def test_every_response_has_a_description(self):
        for path, item in self.spec["paths"].items():
            for method, operation in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                self.assertTrue(operation.get("summary"), f"{method} {path} needs a summary")
                self.assertTrue(operation.get("tags"), f"{method} {path} needs a tag")
                for code, response in operation["responses"].items():
                    self.assertTrue(
                        response.get("description"),
                        f"{method.upper()} {path} -> {code} needs a description",
                    )

    def test_declared_tags_are_all_used(self):
        declared = {tag["name"] for tag in self.spec["tags"]}
        used = {
            tag
            for item in self.spec["paths"].values()
            for operation in item.values()
            if isinstance(operation, dict)
            for tag in operation.get("tags", [])
        }
        self.assertEqual(declared, used, "tag list and operation tags disagree")

    def test_authentication_schemes_match_the_service(self):
        schemes = self.spec["components"]["securitySchemes"]
        self.assertEqual(schemes["apiKey"]["name"], "x-fleet-api-key")
        self.assertEqual(schemes["consoleSession"]["name"], service.CONSOLE_COOKIE)

    def test_endpoints_that_must_stay_open_are_marked_public(self):
        """Sign-in and health cannot require the credential they hand out."""

        for path in ("/", "/api", "/api/session", "/healthz", "/readyz"):
            for method, operation in self.spec["paths"][path].items():
                if method not in {"get", "post"}:
                    continue
                self.assertEqual(
                    operation.get("security"),
                    [],
                    f"{method.upper()} {path} must be documented as unauthenticated",
                )


class DocsEndpointTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        self.client = TestClient(service.app)

    def test_spec_is_served_and_pinned_to_the_requesting_host(self):
        with self.client as client:
            response = client.get("/openapi.json")
            self.assertEqual(response.status_code, 200)
            spec = response.json()
            self.assertEqual(spec["openapi"], "3.1.0")
            self.assertTrue(spec["servers"][0]["url"].startswith("http"))

    def test_reference_page_and_vendored_bundle_are_served(self):
        with self.client as client:
            page = client.get("/docs")
            self.assertEqual(page.status_code, 200)
            self.assertIn("api-reference", page.text)
            self.assertIn("/static/vendor/scalar.standalone.js", page.text)

            bundle = client.get("/static/vendor/scalar.standalone.js")
            self.assertEqual(bundle.status_code, 200)
            self.assertGreater(len(bundle.content), 100_000)

    def test_the_reference_never_loads_a_remote_script(self):
        """The docs share an origin with the authenticated console."""

        with self.client as client:
            page = client.get("/docs")
        for match in re.findall(r'(?:src|href)="([^"]+)"', page.text):
            self.assertFalse(
                match.startswith(("http://", "https://", "//")),
                f"{match} is a remote asset; vendor it instead",
            )

    def test_documentation_is_reachable_without_signing_in(self):
        with self.client as client:
            for path in ("/docs", "/openapi.json"):
                self.assertEqual(client.get(path).status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
