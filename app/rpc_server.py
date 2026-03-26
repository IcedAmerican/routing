"""
routing 容器 RPC 服务
基于 Flask 提供 HTTP JSON-RPC 接口，支持：
  - POST /accuse          写入操作：提交恶意节点指控
  - POST /query_status    读取操作：查询节点黑名单状态
  - GET  /health          健康检查
  - GET  /metrics         性能指标 (内存 / CPU)
  - GET  /sync_status     轻节点同步状态
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


def create_fisco_client() -> FiscoClient:
    """根据环境变量创建 FiscoClient 实例"""
    full_nodes = os.environ.get("FISCO_FULL_NODES", "172.17.0.100:20200")
    first_node = full_nodes.split(",")[0]
    host, port = first_node.rsplit(":", 1)

    group_id = int(os.environ.get("FISCO_GROUP_ID", "1"))
    contract_addr = os.environ.get("BLACKLIST_CONTRACT", "")

    client = FiscoClient(
        channel_host=host,
        channel_port=int(port),
        group_id=group_id,
        contract_address=contract_addr,
    )
    client.connect()
    return client


@app.before_first_request
def init_client():
    global fisco_client
    if fisco_client is None:
        fisco_client = create_fisco_client()


# ======== RPC 接口 ========

@app.route("/accuse", methods=["POST"])
def accuse():
    """
    写入操作：向链上黑名单合约提交指控交易
    请求体: {"node_id": "routing_node3", "reason": "packet_drop_anomaly"}
    响应: {"tx_hash": "0x...", "block_number": 100, "status": "success", "latency_ms": 45.2}
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
    响应: {"node_id": "routing_node3", "status": "ACTIVE"|"BLOCKED", "latency_ms": 12.5}
    """
    data = request.get_json(force=True)
    node_id = data.get("node_id")
    if not node_id:
        return jsonify({"error": "node_id is required"}), 400

    result = fisco_client.query_status(node_id)
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    node_id = os.environ.get("NODE_ID", "unknown")
    connected = fisco_client.is_connected() if fisco_client else False
    return jsonify({
        "node_id": node_id,
        "fisco_connected": connected,
        "timestamp": time.time()
    })


@app.route("/metrics", methods=["GET"])
def metrics():
    """
    性能指标：返回当前轻节点容器的 CPU 和内存占用
    """
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    return jsonify({
        "node_id": os.environ.get("NODE_ID", "unknown"),
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "memory_rss_mb": round(mem_info.rss / (1024 * 1024), 2),
        "memory_vms_mb": round(mem_info.vms / (1024 * 1024), 2),
        "timestamp": time.time()
    })


@app.route("/sync_status", methods=["GET"])
def sync_status():
    """
    轻节点同步状态：返回当前轻节点已同步的区块高度
    可用于测量全节点出块后轻节点的同步延迟
    """
    block_number = None
    latency_ms = 0
    if fisco_client:
        start = time.time()
        block_number = fisco_client.get_block_number()
        latency_ms = round((time.time() - start) * 1000, 2)

    return jsonify({
        "node_id": os.environ.get("NODE_ID", "unknown"),
        "block_number": block_number,
        "query_latency_ms": latency_ms,
        "timestamp": time.time()
    })


def main():
    rpc_port = int(os.environ.get("RPC_PORT", "18545"))
    logger.info(f"starting routing RPC server on port {rpc_port}")
    app.run(host="0.0.0.0", port=rpc_port, threaded=True)


if __name__ == "__main__":
    main()
