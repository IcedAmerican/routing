#!/usr/bin/env python3
"""
routing 模式性能基准测试脚本

通过各 routing 容器的 RPC 端口采集以下性能指标：
1. 黑名单写入延迟 (accuse latency)
2. 黑名单查询延迟 (query latency)
3. 全节点出块后轻节点同步延迟 (sync latency)
4. 轻节点内存与 CPU 占用 (resource usage)

在不同模拟网络条件下对比测试：
- 正常链路 (normal)
- 高延迟链路 (high_delay)
- 链路抖动 (jitter)
- 链路中断恢复 (link_recovery)

用法:
    python benchmark.py --nodes 4 --base-port 18545 [--scenario normal|high_delay|jitter|link_recovery|all]
    python benchmark.py --nodes 4 --base-port 18545 --scenario all --output results.json
"""
import argparse
import json
import os
import sys
import time
import statistics
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any


class RoutingBenchmark:
    """routing 模式性能测试器"""

    def __init__(self, node_count: int, base_port: int, host: str = "127.0.0.1"):
        self.node_count = node_count
        self.base_port = base_port
        self.host = host
        self.endpoints = [
            f"http://{host}:{base_port + i}" for i in range(node_count)
        ]

    def _request(self, url: str, method: str = "GET", json_data: dict = None,
                 timeout: float = 30.0) -> dict:
        """发送 HTTP 请求"""
        try:
            if method == "POST":
                resp = requests.post(url, json=json_data, timeout=timeout)
            else:
                resp = requests.get(url, timeout=timeout)
            return resp.json()
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}

    # ==================== 1. 写入延迟测试 ====================

    def test_accuse_latency(self, rounds: int = 10) -> Dict[str, Any]:
        """
        测试黑名单写入延迟：
        从每个 routing 节点向链上提交指控交易，记录端到端延迟
        """
        print(f"\n{'='*60}")
        print(f"  测试 1: 黑名单写入延迟 (accuse latency)")
        print(f"  节点数: {self.node_count}, 轮次: {rounds}")
        print(f"{'='*60}")

        all_latencies = []
        per_node_latencies = {i: [] for i in range(self.node_count)}

        for r in range(rounds):
            target_node = f"routing_node{(r + 5) % self.node_count}"
            for i, endpoint in enumerate(self.endpoints):
                result = self._request(
                    f"{endpoint}/accuse",
                    method="POST",
                    json_data={
                        "node_id": target_node,
                        "reason": f"benchmark_test_round_{r}"
                    }
                )
                if "latency_ms" in result:
                    lat = result["latency_ms"]
                    all_latencies.append(lat)
                    per_node_latencies[i].append(lat)
                    print(f"  [轮次 {r+1}/{rounds}] 节点 {i} -> accuse({target_node}): "
                          f"{lat:.2f} ms, status={result.get('status', 'N/A')}")
                else:
                    print(f"  [轮次 {r+1}/{rounds}] 节点 {i}: ERROR {result.get('error', 'unknown')}")

        summary = self._compute_stats(all_latencies, "accuse_latency")
        summary["per_node"] = {
            f"node_{i}": self._compute_stats(lats, f"node_{i}")
            for i, lats in per_node_latencies.items() if lats
        }
        self._print_stats(summary, "写入延迟")
        return summary

    # ==================== 2. 查询延迟测试 ====================

    def test_query_latency(self, rounds: int = 20) -> Dict[str, Any]:
        """
        测试黑名单查询延迟：
        从每个 routing 节点查询目标节点状态，记录端到端延迟
        """
        print(f"\n{'='*60}")
        print(f"  测试 2: 黑名单查询延迟 (query latency)")
        print(f"  节点数: {self.node_count}, 轮次: {rounds}")
        print(f"{'='*60}")

        all_latencies = []
        per_node_latencies = {i: [] for i in range(self.node_count)}

        for r in range(rounds):
            target_node = f"routing_node{r % self.node_count}"
            for i, endpoint in enumerate(self.endpoints):
                result = self._request(
                    f"{endpoint}/query_status",
                    method="POST",
                    json_data={"node_id": target_node}
                )
                if "latency_ms" in result:
                    lat = result["latency_ms"]
                    all_latencies.append(lat)
                    per_node_latencies[i].append(lat)
                    print(f"  [轮次 {r+1}/{rounds}] 节点 {i} -> query({target_node}): "
                          f"{lat:.2f} ms, status={result.get('status', 'N/A')}")
                else:
                    print(f"  [轮次 {r+1}/{rounds}] 节点 {i}: ERROR {result.get('error', 'unknown')}")

        summary = self._compute_stats(all_latencies, "query_latency")
        summary["per_node"] = {
            f"node_{i}": self._compute_stats(lats, f"node_{i}")
            for i, lats in per_node_latencies.items() if lats
        }
        self._print_stats(summary, "查询延迟")
        return summary

    # ==================== 3. 同步延迟测试 ====================

    def test_sync_latency(self, duration_sec: int = 30, interval_sec: float = 2.0) -> Dict[str, Any]:
        """
        测试轻节点同步延迟：
        持续轮询各节点的区块高度，检测全节点出块后轻节点同步到该区块的延迟
        """
        print(f"\n{'='*60}")
        print(f"  测试 3: 轻节点同步延迟 (sync latency)")
        print(f"  持续时间: {duration_sec}s, 采样间隔: {interval_sec}s")
        print(f"{'='*60}")

        sync_records = []
        prev_heights = {}
        start_time = time.time()

        while time.time() - start_time < duration_sec:
            for i, endpoint in enumerate(self.endpoints):
                result = self._request(f"{endpoint}/sync_status")
                if "block_number" in result and result["block_number"] is not None:
                    height = result["block_number"]
                    query_lat = result.get("query_latency_ms", 0)

                    if i in prev_heights and height > prev_heights[i]:
                        # 检测到新区块
                        sync_records.append({
                            "node": i,
                            "block_number": height,
                            "query_latency_ms": query_lat,
                            "timestamp": result.get("timestamp", time.time())
                        })
                        print(f"  节点 {i}: 新区块 #{height}, 查询延迟 {query_lat:.2f} ms")

                    prev_heights[i] = height
            time.sleep(interval_sec)

        sync_latencies = [r["query_latency_ms"] for r in sync_records]
        summary = self._compute_stats(sync_latencies, "sync_latency")
        summary["total_new_blocks_detected"] = len(sync_records)
        self._print_stats(summary, "同步延迟")
        return summary

    # ==================== 4. 资源占用测试 ====================

    def test_resource_usage(self, samples: int = 10, interval_sec: float = 3.0) -> Dict[str, Any]:
        """
        测试轻节点 CPU 和内存占用
        """
        print(f"\n{'='*60}")
        print(f"  测试 4: 资源占用 (CPU / Memory)")
        print(f"  采样次数: {samples}, 间隔: {interval_sec}s")
        print(f"{'='*60}")

        per_node_cpu = {i: [] for i in range(self.node_count)}
        per_node_mem = {i: [] for i in range(self.node_count)}

        for s in range(samples):
            for i, endpoint in enumerate(self.endpoints):
                result = self._request(f"{endpoint}/metrics")
                if "cpu_percent" in result:
                    cpu = result["cpu_percent"]
                    mem = result["memory_rss_mb"]
                    per_node_cpu[i].append(cpu)
                    per_node_mem[i].append(mem)
                    print(f"  [采样 {s+1}/{samples}] 节点 {i}: "
                          f"CPU={cpu:.1f}%, MEM={mem:.1f} MB")
            if s < samples - 1:
                time.sleep(interval_sec)

        summary = {
            "test": "resource_usage",
            "per_node": {}
        }
        for i in range(self.node_count):
            if per_node_cpu[i]:
                summary["per_node"][f"node_{i}"] = {
                    "cpu_avg": round(statistics.mean(per_node_cpu[i]), 2),
                    "cpu_max": round(max(per_node_cpu[i]), 2),
                    "mem_avg_mb": round(statistics.mean(per_node_mem[i]), 2),
                    "mem_max_mb": round(max(per_node_mem[i]), 2),
                }
        print(f"\n  资源占用汇总:")
        for node, stats in summary["per_node"].items():
            print(f"    {node}: CPU avg={stats['cpu_avg']}% max={stats['cpu_max']}%, "
                  f"MEM avg={stats['mem_avg_mb']} MB max={stats['mem_max_mb']} MB")
        return summary

    # ==================== 网络条件场景 ====================

    @staticmethod
    def apply_network_scenario(scenario: str, container_prefix: str = "routing_node",
                               node_count: int = 4):
        """
        通过 tc (traffic control) 在容器网络接口上应用不同的模拟场景
        需要在宿主机以 root 权限运行
        """
        scenarios = {
            "normal": {
                "delay": "15ms",
                "loss": "0%",
                "rate": "2Mbit",
                "description": "正常链路: 15ms 延迟, 0% 丢包, 2Mbps 带宽"
            },
            "high_delay": {
                "delay": "200ms",
                "loss": "0%",
                "rate": "2Mbit",
                "description": "高延迟链路: 200ms 延迟, 0% 丢包, 2Mbps 带宽"
            },
            "jitter": {
                "delay": "50ms 30ms distribution normal",
                "loss": "1%",
                "rate": "2Mbit",
                "description": "链路抖动: 50±30ms 延迟(正态分布), 1% 丢包, 2Mbps 带宽"
            },
            "link_recovery": {
                "delay": "15ms",
                "loss": "10%",
                "rate": "512Kbit",
                "description": "链路中断恢复: 15ms 延迟, 10% 丢包, 512Kbps 带宽 (模拟恢复中)"
            },
        }

        if scenario not in scenarios:
            print(f"未知场景: {scenario}, 可选: {list(scenarios.keys())}")
            return

        cfg = scenarios[scenario]
        print(f"\n{'#'*60}")
        print(f"  应用网络场景: {scenario}")
        print(f"  {cfg['description']}")
        print(f"{'#'*60}")

        for i in range(node_count):
            container_name = f"{container_prefix}{i}"
            # 获取容器内第一个非 lo 的接口
            try:
                pid_cmd = f"docker inspect --format '{{{{.State.Pid}}}}' {container_name}"
                pid = subprocess.check_output(pid_cmd, shell=True).decode().strip()
                # 获取接口列表
                iface_cmd = f"nsenter -t {pid} -n ip -o link show | grep -v 'lo:' | head -1 | awk -F: '{{print $2}}'"
                iface = subprocess.check_output(iface_cmd, shell=True).decode().strip()
                if not iface:
                    continue

                # 应用 tc 规则
                tc_cmd = (f"nsenter -t {pid} -n tc qdisc replace dev {iface} root netem "
                          f"delay {cfg['delay']} loss {cfg['loss']} rate {cfg['rate']}")
                subprocess.run(tc_cmd, shell=True, check=True)
                print(f"  {container_name} ({iface}): tc 规则已应用")
            except Exception as e:
                print(f"  {container_name}: 应用失败 - {e}")

    # ==================== 工具方法 ====================

    @staticmethod
    def _compute_stats(latencies: List[float], name: str) -> Dict[str, Any]:
        if not latencies:
            return {"test": name, "count": 0, "error": "no data"}
        return {
            "test": name,
            "count": len(latencies),
            "min_ms": round(min(latencies), 2),
            "max_ms": round(max(latencies), 2),
            "avg_ms": round(statistics.mean(latencies), 2),
            "median_ms": round(statistics.median(latencies), 2),
            "stdev_ms": round(statistics.stdev(latencies), 2) if len(latencies) > 1 else 0,
            "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 2) if len(latencies) >= 20 else None,
        }

    @staticmethod
    def _print_stats(summary: Dict[str, Any], label: str):
        print(f"\n  {label} 汇总:")
        print(f"    样本数: {summary.get('count', 0)}")
        if summary.get("count", 0) > 0:
            print(f"    最小: {summary['min_ms']:.2f} ms")
            print(f"    最大: {summary['max_ms']:.2f} ms")
            print(f"    平均: {summary['avg_ms']:.2f} ms")
            print(f"    中位数: {summary['median_ms']:.2f} ms")
            print(f"    标准差: {summary['stdev_ms']:.2f} ms")
            if summary.get("p95_ms") is not None:
                print(f"    P95: {summary['p95_ms']:.2f} ms")


def run_full_benchmark(bench: RoutingBenchmark, scenario: str) -> Dict[str, Any]:
    """运行完整的基准测试套件"""
    results = {
        "scenario": scenario,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node_count": bench.node_count,
    }

    # 应用网络场景
    RoutingBenchmark.apply_network_scenario(scenario, node_count=bench.node_count)
    time.sleep(5)  # 等待 tc 规则生效

    results["accuse_latency"] = bench.test_accuse_latency(rounds=10)
    results["query_latency"] = bench.test_query_latency(rounds=20)
    results["sync_latency"] = bench.test_sync_latency(duration_sec=30)
    results["resource_usage"] = bench.test_resource_usage(samples=10)

    return results


def main():
    parser = argparse.ArgumentParser(description="routing 模式性能基准测试")
    parser.add_argument("--nodes", type=int, default=4, help="routing 节点数量")
    parser.add_argument("--base-port", type=int, default=18545, help="RPC 起始端口")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="宿主机地址")
    parser.add_argument("--scenario", type=str, default="all",
                        choices=["normal", "high_delay", "jitter", "link_recovery", "all"],
                        help="网络模拟场景")
    parser.add_argument("--output", type=str, default=None, help="输出 JSON 文件路径")
    args = parser.parse_args()

    bench = RoutingBenchmark(
        node_count=args.nodes,
        base_port=args.base_port,
        host=args.host
    )

    # 先做健康检查
    print("="*60)
    print("  routing 模式性能基准测试")
    print("="*60)
    print(f"\n检查节点连通性...")
    for i, endpoint in enumerate(bench.endpoints):
        result = bench._request(f"{endpoint}/health")
        status = "OK" if "node_id" in result else f"FAIL: {result.get('error', 'unknown')}"
        print(f"  节点 {i} ({endpoint}): {status}")

    all_results = []

    if args.scenario == "all":
        for scenario in ["normal", "high_delay", "jitter", "link_recovery"]:
            results = run_full_benchmark(bench, scenario)
            all_results.append(results)
    else:
        results = run_full_benchmark(bench, args.scenario)
        all_results.append(results)

    # 输出结果
    if args.output:
        with open(args.output, "w") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        print(f"\n结果已保存至: {args.output}")

    # 打印对比摘要
    print(f"\n{'='*80}")
    print(f"  对比测试摘要")
    print(f"{'='*80}")
    print(f"{'场景':<20} {'写入延迟(avg)':<18} {'查询延迟(avg)':<18} {'同步延迟(avg)':<18}")
    print(f"{'-'*74}")
    for r in all_results:
        scenario = r["scenario"]
        acc_avg = r["accuse_latency"].get("avg_ms", "N/A")
        qry_avg = r["query_latency"].get("avg_ms", "N/A")
        sync_avg = r["sync_latency"].get("avg_ms", "N/A")
        acc_str = f"{acc_avg:.2f} ms" if isinstance(acc_avg, (int, float)) else acc_avg
        qry_str = f"{qry_avg:.2f} ms" if isinstance(qry_avg, (int, float)) else qry_avg
        sync_str = f"{sync_avg:.2f} ms" if isinstance(sync_avg, (int, float)) else sync_avg
        print(f"{scenario:<20} {acc_str:<18} {qry_str:<18} {sync_str:<18}")


if __name__ == "__main__":
    main()
