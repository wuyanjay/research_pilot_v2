"""用实际镜像和挂载目录测试 HTTP 接口及 A 类交付，会调用真实模型。"""

import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def main():
    project = Path(__file__).resolve().parents[1]
    run_id = uuid.uuid4().hex[:12]
    compose_project = f"rp-v2-smoke-{run_id}"
    container = f"{compose_project}-agent"
    team = project / "test_output/data/JTI"
    evidence = team / "work" / f"container-smoke-{run_id}"
    evidence.mkdir(parents=True)
    payload_path = team / "assigned/A-01.json"
    payload = payload_path.read_bytes()
    inputs = [team / "dispatch.json", *sorted((team / "assigned").glob("*.json"))]
    fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    report = {"image": "research-pilot-v2:test", "case_id": "A-01", "passed": False}

    def request(path, body=None, timeout=300):
        headers = {"Content-Type": "application/json", "X-Research-Task-ID": run_id,
                   "Idempotency-Key": run_id, "X-Challenge-Protocol": "research-assistant.v1"}
        req = urllib.request.Request(base_url + path, data=body, headers=headers)
        # 只请求本次测试容器的本机端口，避免宿主机代理转发本地 HTTP 请求。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        subprocess.run([
            "docker", "compose", "-p", compose_project, "-f", str(project / "compose.test.yaml"),
            "run", "--detach", "--no-deps", "--pull", "never", "--name", container,
            "--publish", "127.0.0.1::8080", "research-pilot",
        ], cwd=project, check=True, capture_output=True, text=True)
        ports = subprocess.check_output([
            "docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container,
        ], text=True)
        port = json.loads(ports)["8080/tcp"][0]["HostPort"]
        base_url = f"http://127.0.0.1:{port}"
        for attempt in range(30):
            try:
                status, health = request("/health", timeout=2)
                if status == 200:
                    break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("容器未正常提供健康检查")
        assert health["status"] == "healthy"
        assert isinstance(health["telemetry_configured"], bool)
        report["health"] = {"http_status": status, "body": health}
        print("GET /health：200，服务正常。", flush=True)

        probe = subprocess.check_output([
            "docker", "exec", container, "python", "-c",
            "import os,json; from pathlib import Path; "
            "print(json.dumps({'uid':os.geteuid(),'env_file_packaged':Path('/app/.env').exists(),"
            "'readonly_mounts':{p:bool(os.statvfs(p).f_flag & os.ST_RDONLY) for p in "
            "('/data','/output/data/JTI/dispatch.json','/output/data/JTI/assigned')},"
            "'output_writable':not bool(os.statvfs('/output').f_flag & os.ST_RDONLY)}))",
        ], text=True)
        report["container"] = json.loads(probe)
        assert report["container"]["uid"] != 0
        assert not report["container"]["env_file_packaged"]
        assert all(report["container"]["readonly_mounts"].values())
        assert report["container"]["output_writable"]

        status, error = request("/solve", body=b"[]")
        assert status == 422
        report["invalid_body"] = {"http_status": status}
        print("POST /solve 非 JSON 对象：422。", flush=True)

        print("开始真实 A-01 模型请求……", flush=True)
        start = time.monotonic()
        status, result = request("/solve", body=payload)
        report["solve"] = {"http_status": status, "elapsed_seconds": round(time.monotonic() - start, 2)}
        (evidence / "solve-response.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        assert status == 200, "A 类请求没有返回成功状态"
        assert set(result) == {"case_id", "answer", "clarifying_question"}
        assert result["case_id"] == "A-01"
        assert all(isinstance(result[key], str) and result[key].strip()
                   for key in ("answer", "clarifying_question"))
        target = team / "A-01.json"
        actual = json.loads(target.read_bytes())
        assert actual == result, "宿主机正式文件与 HTTP 返回不一致"
        report["solve"].update({
            "result_file": str(target.relative_to(project)), "response_matches_file": True,
            "answer_characters": len(result["answer"]),
            "clarifying_question_characters": len(result["clarifying_question"]),
            "result_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        })
        print(f"POST /solve：200，耗时 {report['solve']['elapsed_seconds']} 秒，HTTP 与文件一致。", flush=True)

        original = target.read_bytes()
        conflict = json.loads(payload)
        conflict["title"] += "（冲突请求测试）"
        status, error = request("/solve", body=json.dumps(conflict, ensure_ascii=False).encode("utf-8"))
        assert status == 500
        assert target.read_bytes() == original
        assert "冲突请求测试" not in json.dumps(error, ensure_ascii=False)
        assert not list(team.glob("*.tmp"))
        assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in fingerprints.items())
        report["conflicting_question"] = {"http_status": status, "existing_result_unchanged": True}
        report["inputs_unchanged"] = True
        report["temporary_files_cleaned"] = True
        report["passed"] = True
        print("题目冲突：500，已有结果和只读输入均未改动，无残留临时文件。", flush=True)
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        cleanup = subprocess.run(["docker", "rm", "--force", container], capture_output=True, text=True)
        report["container_removed"] = cleanup.returncode == 0
        # 项目名随机生成，网络只供本次测试使用；不操作已有服务和网络。
        subprocess.run(["docker", "network", "rm", f"{compose_project}_default"], capture_output=True)
        (evidence / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print("验证记录：", evidence.relative_to(project), flush=True)


if __name__ == "__main__":
    main()
