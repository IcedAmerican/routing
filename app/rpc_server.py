"""
routing 容器 RPC 服务
基于 Flask 提供 HTTP JSON-RPC 接口，支持：
  - POST /accuse          写入操作：提交恶意节点指控
  - POST /query_status    读取操作：查询节点黑名单状态
  - GET  /health          健康检查 (含轻节点连接状态)
  - GET  /metrics         性能指标 (轻节点 fisco-bcos 进程的内存/CPU 占用)
  - GET  /sync_status     轻节点区块同步状态
"""
import os
import time
import logging
import psutil
from flask import Flask, request, jsonify

from app.fisco_client import FiscoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)
logger = logging.getLogger("rpc_server")

app = Flask(__name__)
fisco_client: FiscoClient = None
_initialized = False


def create_fisco_client() -> FiscoClient:
    """
    创建 FiscoClient 实例, 连接本地轻节点 JSON-RPC

    架构: Flask RPC -> FiscoClient -> 本地 fisco-bcos 轻节点 (127.0.0.1:20200) -> 全节点网络
    """
    group_id = int(os.environ.get("FISCO_GROUP_ID", "1"))
    contract_addr = os.environ.get("BLACKLIST_CONTRACT", "")

    client = FiscoClient(
        light_node_rpc="http://127.0.0.1:20200",
        group_id=group_id,
        contract_address=contract_addr,
    )
    client.connect()
    return client


@app.before_request
def ensure_init():
    global fisco_client, _initialized
    if not _initialized:
        fisco_client = create_fisco_client()
        _initialized = True


# ======== RPC 接口 ========

@app.route("/accuse", methods=["POST"])
def accuse():
    """
    写入操作：向链上黑名单合约提交指控交易

    请求体: {"node_id": "routing_node3", "reason": "packet_drop_anomaly"}
    响应:   {"tx_hash": "0x...", "block_number": 100, "status": "success", "latency_ms": 45.2}

    流程: Flask -> FiscoClient.accuse() -> 本地轻节点 sendRawTransaction
          -> 轻节点 P2P 转发至全节点 -> 共识打包 -> 回执返回
    """
    data = request.get_json(force=True)
    node_id = data.get("node_id")
    reason = data.get("reason", "unknown")
    if not node_id:
        return jsonify({"error": "node_id is required"}), 400

    result = fisco_client.accuse(node_id, reason)
    return jsonify(result)


@app.route("/query_status", methods=["POST"])
def query_status():
    """
    读取操作：查询目标节点是否在链上黑名单中

    请求体: {"node_id": "routing_node3"}
    响应:   {"node_id": "routing_node3", "status": "ACTIVE"|"BLOCKED", "latency_ms": 12.5}

    流程: Flask -> FiscoClient.query_status() -> 本地轻节点 call (只读)
          -> 轻节点从本地缓存或全节点获取合约状态
    """
    data = request.get_json(force=True)
    node_id = data.get("node_id")
    if not node_id:
        return jsonify({"error": "node_id is required"}), 400

    result = fisco_client.query_status(node_id)
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    """
    健康检查: 包含轻节点连接状态和 fisco-bcos 进程状态
    """
    node_id = os.environ.get("NODE_ID", "unknown")
    connected = fisco_client.is_connected() if fisco_client else False

    # 检查 fisco-bcos 进程是否存活
    fisco_alive = False
    fisco_pid = fisco_client.get_fisco_pid() if fisco_client else None
    if fisco_pid:
        try:
            proc = psutil.Process(fisco_pid)
            fisco_alive = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return jsonify({
        "node_id": node_id,
        "fisco_connected": connected,
        "fisco_process_alive": fisco_alive,
        "fisco_pid": fisco_pid,
        "timestamp": time.time()
    })


@app.route("/metrics", methods=["GET"])
def metrics():
    """
    性能指标：返回 fisco-bcos 轻节点进程和 RPC 服务进程的 CPU/内存占用

    这是测试脚本采集的核心指标之一,
    用于评估轻量级区块链状态同步在星载环境约束下的资源开销。
    """
    node_id = os.environ.get("NODE_ID", "unknown")

    # RPC 服务进程 (Python/Flask)
    rpc_process = psutil.Process(os.getpid())
    rpc_mem = rpc_process.memory_info()

    result = {
        "node_id": node_id,
        "rpc_server": {
            "pid": os.getpid(),
            "cpu_percent": rpc_process.cpu_percent(interval=0.1),
            "memory_rss_mb": round(rpc_mem.rss / (1024 * 1024), 2),
        },
        "timestamp": time.time()
    }

    # fisco-bcos 轻节点进程
    fisco_pid = fisco_client.get_fisco_pid() if fisco_client else None
    if fisco_pid:
        try:
            fisco_proc = psutil.Process(fisco_pid)
            fisco_mem = fisco_proc.memory_info()
            result["fisco_light_node"] = {
                "pid": fisco_pid,
                "cpu_percent": fisco_proc.cpu_percent(interval=0.5),
                "memory_rss_mb": round(fisco_mem.rss / (1024 * 1024), 2),
                "memory_vms_mb": round(fisco_mem.vms / (1024 * 1024), 2),
                "num_threads": fisco_proc.num_threads(),
                "num_fds": fisco_proc.num_fds(),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            result["fisco_light_node"] = {"error": "process not found or access denied"}
    else:
        result["fisco_light_node"] = {"error": "no fisco-bcos process (mock mode)"}

    # 容器总体资源
    result["container_total"] = {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "memory_total_mb": round(psutil.virtual_memory().total / (1024 * 1024), 2),
        "memory_used_mb": round(psutil.virtual_memory().used / (1024 * 1024), 2),
    }

    return jsonify(result)


@app.route("/sync_status", methods=["GET"])
def sync_status():
    """
    轻节点同步状态:
      - block_number: 当前已同步的区块高度
      - sync_info: 同步详情 (本地高度 vs 最高高度)
      - peers: 已连接的全节点数量

    用于测量全节点出块后轻节点同步到该区块的延迟。
    """
    node_id = os.environ.get("NODE_ID", "unknown")
    block_number = None
    sync_info = None
    peers = None
    query_latency_ms = 0

    if fisco_client and fisco_client.is_connected():
        start = time.time()
        block_number = fisco_client.get_block_number()
        query_latency_ms = round((time.time() - start) * 1000, 2)

        sync_info = fisco_client.get_sync_status()
        peers = fisco_client.get_peers()

    return jsonify({
        "node_id": node_id,
        "block_number": block_number,
        "query_latency_ms": query_latency_ms,
        "sync_info": sync_info,
        "connected_peers": len(peers) if isinstance(peers, list) else 0,
        "timestamp": time.time()
    })


def main():
    rpc_port = int(os.environ.get("RPC_PORT", "18545"))
    logger.info(f"starting routing RPC server on port {rpc_port}")
    app.run(host="0.0.0.0", port=rpc_port, threaded=True)


if __name__ == "__main__":
    main()
