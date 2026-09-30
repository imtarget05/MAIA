"""Prompt-library MCP server — the PromptOps artefacts, readable over MCP.

MAIA's prompts are not strings buried in code: ``prompts/**.yaml`` is a
versioned, reviewed, eval-gated library (see ``prompts/README.md``). This server
exposes it over MCP so a client can *discover* the prompts instead of having
them hard-coded in its own instructions.

Two mappings, deliberately different:

* **Prompts** are callable and parameterised: ``prompts/get`` renders the
  declared version into chat messages, and the arguments are validated by
  :func:`maia.promptops.render` (missing or unknown variable → protocol error,
  never a half-filled prompt).
* **Resources** are the same artefacts as plain text, one URI per version
  (``maia://prompts/{name}/{version}``). A client that wants to read or diff the
  raw YAML-as-source-of-truth does not have to go through a render.

Both read from the **same** :class:`PromptRegistry` the rest of the runtime
uses, so a version bump on disk shows up in both without a second source of
truth. Only ``active`` versions are exposed: a draft under review must not be
reachable from a production client, which is the rule PromptOps already
enforces for the runtime.
"""
from __future__ import annotations

from typing import Any

from ...promptops import PromptRegistry, PromptSpec, default_registry, render
from ..protocol import (
    PromptArgument,
    PromptMessage,
    PromptSpecWire,
    ResourceSpec,
)
from ..server import MCPServer

__all__ = ["PROMPT_URI_PREFIX", "build_prompts_server", "prompt_uri"]

PROMPT_URI_PREFIX = "maia://prompts/"


def prompt_uri(name: str, version: str) -> str:
    """Stable URI for one prompt version."""
    return f"{PROMPT_URI_PREFIX}{name}/{version}"


def _spec_to_wire(spec: PromptSpec) -> PromptSpecWire:
    """Advertise a spec's template variables as prompt arguments.

    Every variable is required, because :func:`render` refuses to build a
    prompt with a placeholder left unfilled. Advertising them as optional
    would invite exactly the call that fails.
    """
    variables = sorted(spec.required_variables())
    return PromptSpecWire(
        name=spec.name,
        description=spec.title or spec.description or spec.name,
        arguments=tuple(
            PromptArgument(name=v, description="", required=True) for v in variables
        ),
    )


def _resource_text(spec: PromptSpec) -> str:
    """The prompt rendered as a reviewable document.

    Deliberately not the raw YAML: a client reading a resource wants the text it
    would send, with the guardrail/parameter metadata that the runtime actually
    applies, not a copy of the file whose formatting is a repo concern.
    """
    parameters = spec.parameters.model_dump()
    guardrails = "\n".join(f"- {g}" for g in spec.guardrails) or "- (none declared)"
    return "\n".join([
        f"# {spec.title or spec.name} (v{spec.version}, {spec.status})",
        "",
        spec.description or "",
        "",
        "## Parameters",
        *[f"- {k}: {v}" for k, v in sorted(parameters.items()) if v not in (None, "")],
        "",
        "## Guardrails",
        guardrails,
        "",
        "## System prompt",
        spec.system_prompt.strip(),
        "",
        "## User template",
        spec.user_template.strip(),
    ]) + "\n"


def _build_renderer(spec: PromptSpec):
    def _render(arguments: dict[str, Any]) -> list[PromptMessage]:
        # render() raises ValueError naming the missing variable; the server
        # turns that into INVALID_PARAMS so the caller learns which argument to
        # fix instead of receiving a prompt with a literal "{question}" in it.
        rendered = render(spec, arguments)
        return [
            PromptMessage(role=m["role"], text=m["content"])
            for m in rendered.messages
        ]

    return _render


def build_prompts_server(
    *,
    registry: PromptRegistry | None = None,
    name: str = "prompts",
    version: str = "1.0.0",
) -> MCPServer:
    """Build the prompt-library server.

    ``registry`` is injectable so tests (and an operator previewing a candidate
    prompt directory) can point at a different root without touching
    ``settings.PROMPTS_DIR``.
    """
    reg = registry if registry is not None else default_registry()
    server = MCPServer(
        name,
        version=version,
        title="MAIA prompt library",
        instructions=(
            "Versioned, eval-gated prompts. Call prompts/list to discover them, "
            "then prompts/get with every required argument."
        ),
    )

    for prompt_name in reg.names():
        # One MCP prompt per *name* (the spec keys prompts/list by name), so
        # this is the current release — the newest ``active`` version, falling
        # back to the newest overall. Older active versions stay reachable as
        # resources below, which is where a client would go to compare.
        spec = reg.current(prompt_name)
        if spec is None:
            continue
        if spec.status != "active":
            # Draft/deprecated stay out of reach: the runtime already refuses to
            # resolve them, and MCP must not be a way around that.
            continue
        server.add_prompt(_spec_to_wire(spec), _build_renderer(spec))

        # Every active version is readable, so a client can diff a prompt
        # against its predecessor without leaving MCP.
        for version_spec in reg.versions(prompt_name):
            if version_spec.status != "active":
                continue
            server.add_resource(
                ResourceSpec(
                    uri=prompt_uri(version_spec.name, version_spec.version),
                    name=f"{version_spec.name}@{version_spec.version}",
                    description=version_spec.title or version_spec.description
                    or version_spec.name,
                    mime_type="text/markdown",
                ),
                _resource_text(version_spec),
            )
    return server
