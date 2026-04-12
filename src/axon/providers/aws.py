"""AWS provider implementation — Lambda, ECS/Fargate."""

from __future__ import annotations

import asyncio
import json
import os
import time
import zipfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from axon.exceptions import AuthError, DeploymentError, ProviderError
from axon.providers.base import IAxonProvider
from axon.types import (
    CostEstimate,
    Deployment,
    DeploymentConfig,
    HealthStatus,
    Message,
    ProviderHealth,
)

# Lambda pricing (USD per 1M requests + GB-second)
_LAMBDA_PRICE_PER_REQUEST = 0.0000002
_LAMBDA_PRICE_PER_GB_SEC = 0.0000166667


class AWSProvider(IAxonProvider):
    """
    AWS cloud provider — Lambda (serverless) and ECS/Fargate (containers).

    Defaults to Lambda for quick serverless deploys. Set
    ``metadata.service = "fargate"`` in DeploymentConfig to use ECS/Fargate.

    Requires:
        AWS_ACCESS_KEY_ID      — IAM access key
        AWS_SECRET_ACCESS_KEY  — IAM secret key
        AWS_REGION             — AWS region (default: us-east-1)

    Optional:
        AWS_LAMBDA_ROLE_ARN    — IAM role ARN for Lambda execution
        AWS_ECS_CLUSTER        — ECS cluster name (default: axon)
        AWS_ECR_REPO           — ECR repository URI for Fargate image pushes
    """

    def __init__(self) -> None:
        self._boto_session: Any | None = None   # boto3.Session
        self._region: str = "us-east-1"
        self._connected: bool = False
        self._function_urls: dict[str, str] = {}   # function_name -> invoke URL
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "aws"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Authenticate using boto3 with AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
        or any other credential chain boto3 supports (instance profile, SSO, etc.).
        """
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ProviderError(
                "aws",
                "boto3 is required. Install with: pip install axon[aws]",
            ) from exc

        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
        self._region = os.environ.get("AWS_REGION", "us-east-1")

        if access_key and secret:
            self._boto_session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret,
                aws_secret_access_key_id=None,
                region_name=self._region,
            )
        else:
            # Fall back to default credential chain (IAM role, SSO, ~/.aws/credentials)
            self._boto_session = boto3.Session(region_name=self._region)

        # Validate by calling STS get-caller-identity
        try:
            sts = self._boto_session.client("sts")
            await asyncio.get_event_loop().run_in_executor(None, sts.get_caller_identity)
        except Exception as exc:
            raise AuthError(f"AWS authentication failed: {exc}") from exc

        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._boto_session = None

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy a workload to AWS.

        For Python/Node.js entry points → Lambda (zip deploy).
        Set config.metadata['service'] = 'fargate' for ECS/Fargate container deploy.
        """
        if not self._connected or not self._boto_session:
            raise ProviderError("aws", "Not connected. Call connect() first.")

        service = config.metadata.get("service", "lambda")

        if service == "fargate":
            return await self._deploy_fargate(config)
        return await self._deploy_lambda(config)

    async def _deploy_lambda(self, config: DeploymentConfig) -> Deployment:
        """Package entry point as a Lambda zip and create/update the function."""
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("aws", f"Entry point not found: {entry}")

        function_name = _sanitise_name(config.name)
        role_arn = os.environ.get("AWS_LAMBDA_ROLE_ARN", "")
        if not role_arn:
            raise DeploymentError(
                "aws",
                "AWS_LAMBDA_ROLE_ARN required for Lambda deploys. "
                "Create an execution role and set it in your .env.",
            )

        # Build zip in memory
        zip_path = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _build_lambda_zip(entry, config)
        )

        try:
            lambda_client = self._boto_session.client("lambda", region_name=self._region)

            runtime_map = {"nodejs": "nodejs20.x", "python": "python3.11", "docker": "python3.11"}
            runtime = runtime_map.get(config.runtime.value, "python3.11")
            handler = "index.handler" if config.runtime.value == "nodejs" else "handler.handler"

            zip_bytes = zip_path.read_bytes()

            def _create_or_update() -> dict[str, Any]:
                try:
                    resp = lambda_client.create_function(
                        FunctionName=function_name,
                        Runtime=runtime,
                        Role=role_arn,
                        Handler=handler,
                        Code={"ZipFile": zip_bytes},
                        Description=f"Axon deployment: {config.name}",
                        Timeout=config.timeout_ms // 1000,
                        MemorySize=config.memory_mb,
                        Environment={"Variables": _filter_env(config.env)},
                        Publish=True,
                    )
                    return resp
                except lambda_client.exceptions.ResourceConflictException:
                    return lambda_client.update_function_code(
                        FunctionName=function_name,
                        ZipFile=zip_bytes,
                        Publish=True,
                    )

            data = await asyncio.get_event_loop().run_in_executor(None, _create_or_update)

            function_arn: str = data.get("FunctionArn", "")

            # Create/retrieve function URL for HTTP invoke
            def _get_or_create_url() -> str:
                try:
                    url_resp = lambda_client.get_function_url_config(FunctionName=function_name)
                    return url_resp["FunctionUrl"]
                except Exception:
                    url_resp = lambda_client.create_function_url_config(
                        FunctionName=function_name,
                        AuthType="NONE",
                    )
                    # Allow public invoke
                    lambda_client.add_permission(
                        FunctionName=function_name,
                        StatementId="AllowPublicAccess",
                        Action="lambda:InvokeFunctionUrl",
                        Principal="*",
                        FunctionUrlAuthType="NONE",
                    )
                    return url_resp["FunctionUrl"]

            function_url = await asyncio.get_event_loop().run_in_executor(None, _get_or_create_url)
            self._function_urls[function_name] = function_url

            return Deployment(
                id=function_name,
                name=config.name,
                provider="aws",
                status="active",
                created_at=datetime.now(timezone.utc),
                endpoint=function_url,
                metadata={
                    "service": "lambda",
                    "function_arn": function_arn,
                    "region": self._region,
                    "runtime": runtime,
                },
            )

        except Exception as exc:
            raise DeploymentError("aws", f"Lambda deploy failed: {exc}") from exc
        finally:
            zip_path.unlink(missing_ok=True)

    async def _deploy_fargate(self, config: DeploymentConfig) -> Deployment:
        """Deploy a container workload to ECS/Fargate."""
        cluster = os.environ.get("AWS_ECS_CLUSTER", "axon")
        ecr_repo = os.environ.get("AWS_ECR_REPO", "")

        if not ecr_repo:
            raise DeploymentError(
                "aws",
                "AWS_ECR_REPO required for Fargate deploys. "
                "Create an ECR repository and set its URI in your .env.",
            )

        task_name = _sanitise_name(config.name)

        ecs = self._boto_session.client("ecs", region_name=self._region)

        def _register_task() -> str:
            resp = ecs.register_task_definition(
                family=task_name,
                networkMode="awsvpc",
                requiresCompatibilities=["FARGATE"],
                cpu=str(max(256, config.metadata.get("cpu_units", 512))),
                memory=str(config.memory_mb),
                containerDefinitions=[{
                    "name": task_name,
                    "image": f"{ecr_repo}:latest",
                    "essential": True,
                    "environment": [
                        {"name": k, "value": v}
                        for k, v in _filter_env(config.env).items()
                    ],
                    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
                }],
            )
            return resp["taskDefinition"]["taskDefinitionArn"]

        task_arn = await asyncio.get_event_loop().run_in_executor(None, _register_task)

        def _run_task() -> dict[str, Any]:
            subnet_ids: list[str] = config.metadata.get("subnet_ids", [])
            sg_ids: list[str] = config.metadata.get("security_group_ids", [])
            return ecs.run_task(
                cluster=cluster,
                taskDefinition=task_arn,
                launchType="FARGATE",
                count=config.replicas,
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnet_ids,
                        "securityGroups": sg_ids,
                        "assignPublicIp": "ENABLED",
                    }
                },
            )

        task_resp = await asyncio.get_event_loop().run_in_executor(None, _run_task)
        task_id = task_resp["tasks"][0]["taskArn"].split("/")[-1] if task_resp.get("tasks") else ""

        return Deployment(
            id=task_id or task_name,
            name=config.name,
            provider="aws",
            status="pending",
            created_at=datetime.now(timezone.utc),
            endpoint=None,
            metadata={
                "service": "fargate",
                "cluster": cluster,
                "task_arn": task_arn,
                "region": self._region,
            },
        )

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        Invoke a Lambda function URL with the payload.
        For Fargate, POST to the task's public endpoint.
        """
        import httpx

        endpoint = self._function_urls.get(processor_id)
        if not endpoint:
            raise ProviderError(
                "aws",
                f"No invoke URL for {processor_id}. Did you deploy first?",
            )

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(endpoint, json={"payload": payload})
            resp.raise_for_status()
            try:
                result = resp.json()
            except Exception:
                result = resp.text

        msg = Message(processor_id=processor_id, payload=result)
        for handler in list(self._message_handlers):
            handler(msg)

    def on_message(self, handler: Callable[[Message], None]) -> Callable[[], None]:
        self._message_handlers.append(handler)
        return lambda: self._message_handlers.remove(handler)

    # ------------------------------------------------------------------
    # Listings & health
    # ------------------------------------------------------------------

    async def list_deployments(self) -> list[Deployment]:
        if not self._connected or not self._boto_session:
            raise ProviderError("aws", "Not connected.")

        lambda_client = self._boto_session.client("lambda", region_name=self._region)

        def _list() -> list[dict[str, Any]]:
            resp = lambda_client.list_functions()
            return [
                f for f in resp.get("Functions", [])
                if f.get("Description", "").startswith("Axon deployment:")
            ]

        functions = await asyncio.get_event_loop().run_in_executor(None, _list)
        return [
            Deployment(
                id=fn["FunctionName"],
                name=fn.get("Description", "").replace("Axon deployment: ", ""),
                provider="aws",
                status="active",
                created_at=datetime.fromisoformat(
                    fn["LastModified"].replace("Z", "+00:00")
                ) if "LastModified" in fn else datetime.now(timezone.utc),
                endpoint=self._function_urls.get(fn["FunctionName"]),
                metadata={"service": "lambda", "arn": fn["FunctionArn"]},
            )
            for fn in functions
        ]

    async def health(self) -> ProviderHealth:
        """Probe Lambda service endpoint for the configured region."""
        import httpx
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://lambda.{self._region}.amazonaws.com/2015-03-31/functions"
                )
            latency_ms = (time.monotonic() - start) * 1000
            # 403 = reachable but unauthorized — that means Lambda endpoint is up
            status = HealthStatus.HEALTHY if resp.status_code in (200, 403) else HealthStatus.DEGRADED
            return ProviderHealth(provider="aws", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="aws", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        duration_s = config.timeout_ms / 1000
        memory_gb = config.memory_mb / 1024
        gb_seconds = duration_s * memory_gb * config.replicas
        compute_cost = gb_seconds * _LAMBDA_PRICE_PER_GB_SEC
        request_cost = config.replicas * _LAMBDA_PRICE_PER_REQUEST
        total = compute_cost + request_cost
        return CostEstimate(
            provider="aws",
            token="USD",
            amount=total,
            usd_estimate=total,
            per_hour=False,
            breakdown={
                "compute_gb_seconds": gb_seconds,
                "compute_cost": compute_cost,
                "request_cost": request_cost,
            },
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_MNEMONIC", "_PRIVATE_KEY")


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not any(k.upper().endswith(s) for s in _SECRET_SUFFIXES)}


def _sanitise_name(name: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9\-_]", "-", name)[:64].strip("-") or "axon-function"


def _build_lambda_zip(entry: Path, config: DeploymentConfig) -> Path:
    """Build a Lambda deployment zip containing the entry file."""
    safe_env = _filter_env(config.env)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", prefix="axon-aws-", delete=False)
    tmp.close()
    zip_path = Path(tmp.name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        source = entry.read_text(encoding="utf-8")

        if config.runtime.value == "nodejs":
            preamble = "".join(
                f'process.env["{k}"] = {json.dumps(v)};\n' for k, v in safe_env.items()
            )
            zf.writestr("index.js", preamble + source)
        else:
            # Python — wrap in handler.py with Lambda handler signature
            env_lines = "".join(
                f'    os.environ["{k}"] = {json.dumps(v)}\n' for k, v in safe_env.items()
            )
            wrapper = (
                "import os, json\n\n"
                + source
                + "\n\n"
                "def handler(event, context):\n"
                + env_lines
                + "    payload = event.get('payload', event)\n"
                "    result = handle_message(payload)\n"
                "    return {'statusCode': 200, 'body': json.dumps(result)}\n"
            )
            zf.writestr("handler.py", wrapper)

    return zip_path
