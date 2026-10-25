"""Manual diagnostic check for local hardware, Ollama server, model, and capabilities."""

import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Optional

# Ensure project root is in sys.path when executed directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from config import settings
from core.ollama_provider import (
    OllamaProvider,
    list_models,
    server_version,
)
from security.permissions import PermissionLevel
from tools.registry import Tool


def check_gpu() -> tuple[str, str]:
    """Stage 1: Query nvidia-smi for GPU name and total memory."""
    print("\n--- Stage 1: GPU Detection (nvidia-smi) ---")
    try:
        res = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
        )
        lines = [line.strip() for line in res.strip().splitlines() if line.strip()]
        if lines:
            for line in lines:
                parts = [p.strip() for p in line.split(",", 1)]
                name = parts[0]
                vram = parts[1] if len(parts) > 1 else "unknown VRAM"
                print(f"  GPU detected: {name} (Memory: {vram})")
            print("[STAGE 1: GPU] PASS")
            return "PASS", lines[0]
        else:
            print("  WARN: No GPU listed by nvidia-smi. The model will run on CPU.")
            print("[STAGE 1: GPU] WARN")
            return "WARN", "CPU (nvidia-smi empty)"
    except (FileNotFoundError, subprocess.SubprocessError, Exception) as exc:
        print(f"  WARN: nvidia-smi not found or failed ({exc}). The model will run on CPU.")
        print("[STAGE 1: GPU] WARN")
        return "WARN", "CPU (no nvidia-smi)"


def check_server(host: str) -> tuple[str, Optional[str]]:
    """Stage 2: Ollama server reachability and version."""
    print(f"\n--- Stage 2: Ollama Server Reachability ({host}) ---")
    try:
        ver = server_version(host)
        if ver:
            print(f"  Ollama server is reachable. Version: {ver}")
            print("[STAGE 2: OLLAMA SERVER] PASS")
            return "PASS", ver
        else:
            print(f"  Ollama server at {host} returned no version.")
            print("  Start it with the Ollama app or run: ollama serve")
            print("[STAGE 2: OLLAMA SERVER] FAIL")
            return "FAIL", None
    except Exception as exc:
        print(f"  Ollama server check failed: {exc}")
        print("  Start it with the Ollama app or run: ollama serve")
        print("[STAGE 2: OLLAMA SERVER] FAIL")
        return "FAIL", None


def check_model(host: str, model: str) -> tuple[str, bool]:
    """Stage 3: Configured model installed."""
    print(f"\n--- Stage 3: Model Installation Check ('{model}') ---")
    try:
        models = list_models(host)
        if models is None:
            print(f"  Cannot list models from {host}. Is Ollama running?")
            print(f"  To install '{model}', run: ollama pull {model}")
            print("[STAGE 3: MODEL CHECK] FAIL")
            return "FAIL", False

        # Match model by exact name or prefix (e.g. qwen3:8b matches qwen3:8b or qwen3:8b:latest)
        base_name = model.split(":")[0].lower()
        matched = False
        for m in models:
            m_lower = m.lower()
            if m_lower == model.lower() or m_lower.startswith(f"{model.lower()}:") or m_lower.split(":")[0] == base_name:
                matched = True
                print(f"  Model '{model}' is installed (found '{m}').")
                break

        if matched:
            print("[STAGE 3: MODEL CHECK] PASS")
            return "PASS", True
        else:
            print(f"  Model '{model}' is NOT installed in Ollama.")
            print(f"  Available models: {', '.join(models) if models else '(none)'}")
            print(f"  To install, run: ollama pull {model}")
            print("[STAGE 3: MODEL CHECK] FAIL")
            return "FAIL", False
    except Exception as exc:
        print(f"  Model check failed: {exc}")
        print(f"  To install, run: ollama pull {model}")
        print("[STAGE 3: MODEL CHECK] FAIL")
        return "FAIL", False


def check_generation(provider: OllamaProvider, host: str, model: str) -> tuple[str, dict[str, Any]]:
    """Stage 4: Warmup time and 60-token streamed generation with TTFT and tokens/s."""
    print("\n--- Stage 4: Warmup & 60-Token Generation ---")
    stats: dict[str, Any] = {}
    try:
        # Warmup
        print("  Running warmup (num_predict=1)...")
        warmup_s = provider.warmup()
        print(f"  Warmup completed in {warmup_s:.2f}s.")
        stats["warmup_s"] = warmup_s

        # 60-token streamed generation
        print("  Streaming 60-token generation...")
        url = f"{host.rstrip('/')}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Write a short creative poem about stars."}],
            "stream": True,
            "options": {
                "num_predict": 60,
                "num_ctx": provider.num_ctx,
                "temperature": 0.2,
            },
        }

        t_req_start = time.perf_counter()
        resp = requests.post(url, json=payload, stream=True, timeout=120)
        if resp.status_code != 200:
            print(f"  Server error ({resp.status_code}): {resp.text[:200]}")
            print("[STAGE 4: GENERATION PERFORMANCE] FAIL")
            return "FAIL", stats

        ttft: Optional[float] = None
        eval_count = 0
        eval_duration_ns = 0
        total_text = ""

        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8") if isinstance(line, (bytes, bytearray)) else line
            if not line_str.strip():
                continue
            try:
                chunk = json.loads(line_str)
            except Exception:
                continue

            content = chunk.get("message", {}).get("content", "")
            if content and ttft is None:
                ttft = time.perf_counter() - t_req_start

            total_text += content

            if chunk.get("done", False):
                eval_count = chunk.get("eval_count", 0)
                eval_duration_ns = chunk.get("eval_duration", 0)
                break

        resp.close()

        ttft_s = ttft if ttft is not None else (time.perf_counter() - t_req_start)
        if eval_duration_ns > 0:
            tps = eval_count / (eval_duration_ns / 1e9)
        else:
            gen_time = max(0.001, (time.perf_counter() - t_req_start) - ttft_s)
            tps = eval_count / gen_time if eval_count > 0 else 0.0

        stats["ttft_s"] = ttft_s
        stats["eval_count"] = eval_count
        stats["eval_duration_s"] = eval_duration_ns / 1e9
        stats["tokens_per_sec"] = tps

        print(f"  Time to first token (TTFT): {ttft_s:.2f}s")
        print(f"  Generated tokens: {eval_count}")
        print(f"  Tokens per second: {tps:.1f} tokens/s")
        print("[STAGE 4: GENERATION PERFORMANCE] PASS")
        return "PASS", stats
    except Exception as exc:
        print(f"  Generation failed: {exc}")
        print("[STAGE 4: GENERATION PERFORMANCE] FAIL")
        return "FAIL", stats


def check_tool_calls(provider: OllamaProvider) -> tuple[str, int]:
    """Stage 5: Tool-call test with get_weather(city) repeated 3 times."""
    print("\n--- Stage 5: Tool Calling (3 attempts) ---")
    weather_tool = Tool(
        name="get_weather",
        description="Get current weather for a city",
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name",
                }
            },
            "required": ["city"],
        },
        func=lambda city="": f"Sunny in {city}",
        permission=PermissionLevel.SAFE,
    )

    passes = 0
    prompt = "What's the weather in Paris?"

    for i in range(1, 4):
        try:
            resp = provider.generate(
                messages=[{"role": "user", "content": prompt}],
                tools=[weather_tool],
            )
            # Check structured tool call
            valid = False
            for tc in resp.tool_calls:
                if tc.name == "get_weather" and isinstance(tc.args, dict):
                    city_arg = str(tc.args.get("city", "")).lower()
                    if "paris" in city_arg:
                        valid = True
                        break

            if valid:
                passes += 1
                print(f"  Run {i}/3: PASS (tool_call=get_weather(city='Paris'))")
            else:
                tc_repr = resp.tool_calls[0].args if resp.tool_calls else f"text only: '{resp.text[:60]}'"
                print(f"  Run {i}/3: FAIL (received {tc_repr})")
        except Exception as exc:
            print(f"  Run {i}/3: FAIL (exception: {exc})")

    print(f"  Tool-calling success rate: {passes}/3 passed.")
    if passes == 3:
        print("[STAGE 5: TOOL CALLING] PASS")
        return "PASS", passes
    else:
        print(f"[STAGE 5: TOOL CALLING] FAIL ({passes}/3 passed)")
        return "FAIL", passes


def check_context(provider: OllamaProvider) -> tuple[str, bool]:
    """Stage 6: Context test (roughly 70% of OLLAMA_NUM_CTX tokens) checking for hidden fact."""
    print("\n--- Stage 6: Context Retention Test (~70% of num_ctx) ---")
    try:
        num_ctx = provider.num_ctx
        target_tokens = int(num_ctx * 0.70)
        # Average English token is roughly 0.75 words / 4 chars.
        # Repeat filler text to reach roughly 70% of context window.
        filler_sentence = "The orbit assistant manages desktop tasks with automated tools and high precision. "
        sentences_needed = max(50, target_tokens // 18)
        filler = filler_sentence * sentences_needed

        code_word = "PELICAN"
        prompt = (
            f"The secret code word is {code_word}. Remember this code word.\n\n"
            f"{filler}\n\n"
            "What is the secret code word? State only the code word."
        )

        approx_words = len(prompt.split())
        print(f"  Sending prompt with ~{approx_words} words (target tokens: ~{target_tokens})...")

        resp = provider.generate(messages=[{"role": "user", "content": prompt}])
        reply = resp.text.strip()

        if code_word in reply.upper():
            print(f"  Model correctly recalled '{code_word}' across long context.")
            print("[STAGE 6: CONTEXT RETENTION] PASS")
            return "PASS", True
        else:
            print(f"  Model failed to recall '{code_word}'. Reply: '{reply[:120]}'")
            print("[STAGE 6: CONTEXT RETENTION] FAIL")
            return "FAIL", False
    except Exception as exc:
        print(f"  Context test failed: {exc}")
        print("[STAGE 6: CONTEXT RETENTION] FAIL")
        return "FAIL", False


def main() -> None:
    print("=" * 60)
    print("ORBIT Local Environment & Model Diagnostic Check")
    print("=" * 60)

    host = settings.OLLAMA_HOST
    model = settings.OLLAMA_MODEL
    print(f"Configured Ollama Host:  {host}")
    print(f"Configured Ollama Model: {model}")
    print(f"Configured Context Size: {settings.OLLAMA_NUM_CTX}")

    # Stage 1: GPU
    gpu_status, gpu_info = check_gpu()

    # Stage 2: Server
    server_status, s_ver = check_server(host)

    # Stage 3: Model
    model_status, _ = check_model(host, model)

    # Stage 4: Generation performance
    gen_status = "FAIL"
    provider: Optional[OllamaProvider] = None
    if server_status == "PASS" and model_status == "PASS":
        try:
            provider = OllamaProvider(
                model=model,
                host=host,
                num_ctx=settings.OLLAMA_NUM_CTX,
                temperature=settings.OLLAMA_TEMPERATURE,
                seed=settings.OLLAMA_SEED,
                think=settings.OLLAMA_THINK,
                keep_alive=settings.OLLAMA_KEEP_ALIVE,
                timeout_s=settings.OLLAMA_TIMEOUT_S,
                allow_remote=settings.OLLAMA_ALLOW_REMOTE,
                inline_tools=settings.OLLAMA_INLINE_TOOLS,
            )
        except Exception as exc:
            print(f"  Could not initialize OllamaProvider: {exc}")

    if provider:
        gen_status, _ = check_generation(provider, host, model)
    else:
        print("\n--- Stage 4: Warmup & 60-Token Generation ---")
        print("  Skipped: Ollama server or model not available.")
        print("[STAGE 4: GENERATION PERFORMANCE] FAIL")

    # Stage 5: Tool calls
    tool_status = "FAIL"
    tool_passes = 0
    if provider:
        tool_status, tool_passes = check_tool_calls(provider)
    else:
        print("\n--- Stage 5: Tool Calling (3 attempts) ---")
        print("  Skipped: Ollama server or model not available.")
        print("[STAGE 5: TOOL CALLING] FAIL")

    # Stage 6: Context test
    ctx_status = "FAIL"
    if provider:
        ctx_status, _ = check_context(provider)
    else:
        print("\n--- Stage 6: Context Retention Test ---")
        print("  Skipped: Ollama server or model not available.")
        print("[STAGE 6: CONTEXT RETENTION] FAIL")

    # Stage 7: Final Summary
    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    print(f"1. GPU Detection:          {gpu_status} ({gpu_info})")
    print(f"2. Ollama Server:          {server_status} (version: {s_ver or 'N/A'})")
    print(f"3. Model Installation:     {model_status} ({model})")
    print(f"4. Generation Performance: {gen_status}")
    print(f"5. Tool Calling:           {tool_status} ({tool_passes}/3 passed)")
    print(f"6. Context Retention:      {ctx_status}")

    if tool_passes < 3:
        print("\n" + "!" * 60)
        print(f"WARNING: Tool-call passes are {tool_passes}/3 (< 3 of 3).")
        print("The configured local model may struggle with structured tool calls.")
        print("Consider pulling a larger or more capable function-calling model")
        print("(e.g., 'qwen2.5:14b', 'qwen2.5:7b', or 'llama3.1:8b').")
        print("!" * 60)

    print("=" * 60)


if __name__ == "__main__":
    main()
