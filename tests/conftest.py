"""Isolated HTTP protocol fixtures. These responses never enter a release runtime."""

import json
from collections import deque

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from vidu_purchase.billing_gateway import GatewaySettings, HttpCommerceGateway
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.store import Store
from vidu_purchase.video_provider import AccountBinding, WebsiteVideoProvider


class ProtocolFixture:
    def __init__(self):
        self.calls = []
        self.requests = {}
        self.credits = 0
        self.payment = 0
        self.receipt_steps = deque()
        self.order_changes = {}
        self.checkout_changes = {}
        self.checkout_timeout = False
        self.plan = {
            "plan_id": "fixture-monthly",
            "name": "Protocol fixture — not a real offer",
            "currency": "USD",
            "billing_cycle": "monthly",
            "amount_minor": 987,
            "renewal_amount_minor": 1150,
            "credits": 80,
            "recurring": True,
            "source": "vidu_website",
            "source_text": "Test response only",
        }

    def billing(self, request):
        self.calls.append(request)
        assert request.headers["authorization"] == "Bearer test-only-gateway-secret"
        if request.url.path == "/v1/catalog":
            return httpx.Response(
                200,
                json={"account_ref": request.url.params["account_ref"], "plans": [self.plan]},
            )
        if request.method == "POST":
            body = json.loads(request.content)
            self.requests[body["request_id"]] = body
            if self.checkout_timeout:
                raise httpx.ReadTimeout("private-token-and-body")
            return httpx.Response(
                200,
                json={
                    **{
                        key: body[key]
                        for key in (
                            "account_ref",
                            "request_id",
                            "plan_id",
                            "amount_minor",
                            "currency",
                            "billing_cycle",
                            "recurring",
                            "renewal_amount_minor",
                        )
                    },
                    "checkout_url": "https://checkout.stripe.com/c/pay/cs_live_TestFixtureOnly",
                    "checkout_reference": "cs_live_TestFixtureOnly",
                    **self.checkout_changes,
                },
            )
        body = self.requests[request.url.path.rsplit("/", 1)[1]]
        payment = self.receipt_steps.popleft() if self.receipt_steps else self.payment
        if payment == 0:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                **{
                    key: body[key]
                    for key in ("account_ref", "request_id", "plan_id", "amount_minor", "currency")
                },
                "order_reference": "fixture-order-" + body["request_id"],
                "payment_succeeded": True,
                "entitlement_active": payment == 2,
                **self.order_changes,
            },
        )

    def video(self, request):
        self.calls.append(request)
        assert request.headers["authorization"] == "Token test-only-video-secret"
        path = request.url.path
        if path.endswith("credits/me"):
            return httpx.Response(200, json={"credits": self.credits})
        if path.endswith("claw-pass/status"):
            return httpx.Response(200, json={"has_pass": False})
        if path.endswith("tasks/credits"):
            return httpx.Response(
                200,
                json={
                    "cost_credits": 20,
                    "current_credits": self.credits,
                    "can_submit": self.credits >= 20,
                },
            )
        if request.method == "POST":
            return httpx.Response(200, json={"id": "fixture-task-123"})
        return httpx.Response(200, json={"id": "fixture-task-123", "state": "success"})

    def post_count(self, path):
        return sum(call.method == "POST" and call.url.path == path for call in self.calls)


@pytest.fixture
def protocol_system(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDU_COMMERCE_TEST_TOKEN", "test-only-gateway-secret")
    monkeypatch.setenv("VIDU_ACCOUNT_TEST_TOKEN", "test-only-video-secret")
    fixture = ProtocolFixture()
    verifier = StaticTokenVerifier(
        tokens={
            "alice": {
                "client_id": "shared-client",
                "sub": "alice",
                "scopes": ["vidu:purchase", "vidu:video"],
            },
            "bob": {
                "client_id": "shared-client",
                "sub": "bob",
                "scopes": ["vidu:purchase", "vidu:video"],
            },
            "purchase-only": {
                "client_id": "shared-client",
                "sub": "alice",
                "scopes": ["vidu:purchase"],
            },
            "no-subject": {"client_id": "shared-client", "scopes": ["vidu:purchase"]},
            "expired": {
                "client_id": "shared-client",
                "sub": "alice",
                "expires_at": 1,
                "scopes": ["vidu:purchase"],
            },
        }
    )
    billing = HttpCommerceGateway(
        GatewaySettings(
            base_url="https://billing.example.invalid",
            token_env="VIDU_COMMERCE_TEST_TOKEN",
            account_refs={"alice": "fixture-account-alice", "bob": "fixture-account-bob"},
        ),
        transport=httpx.MockTransport(fixture.billing),
    )
    video = WebsiteVideoProvider(
        {"alice": AccountBinding(token_env="VIDU_ACCOUNT_TEST_TOKEN")},
        generation_enabled=True,
        transport=httpx.MockTransport(fixture.video),
    )
    store = Store(tmp_path / "hosted.sqlite3")
    app = create_hosted_app(
        store=store,
        verifier=verifier,
        video=video,
        billing=billing,
        poll_interval=0.005,
        stream_seconds=0.02,
    )
    return app, fixture, verifier, store, video, billing
