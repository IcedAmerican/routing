"""
FISCO-BCOS 轻节点客户端封装

架构说明:
  ┌─────────────────────────────────┐
  │     routing 容器                │
  │  ┌───────────┐  ┌────────────┐ │
  │  │ RPC Server│──│FiscoClient │ │
  │  │ (Flask)   │  │(本模块)     │ │
  │  └───────────┘  └─────┬──────┘ │
  │                       │JSON-RPC│
  │               ┌───────▼──────┐ │
  │               │ fisco-bcos   │ │
  │               │ (轻节点进程)  │ │
  │               └───────┬──────┘ │
  └───────────────────────┼────────┘
                          │P2P
                  ┌───────▼──────┐
                  │ 全节点网络    │
                  │ (手动部署)    │
                  └──────────────┘

FiscoClient 通过本地轻节点 JSON-RPC (127.0.0.1:20200) 交互,
轻节点负责与全节点的 P2P 通信、区块同步等底层工作。
"""
import json
import os
import time
import logging
import subprocess
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 黑名单合约 ABI
BLACKLIST_ABI = [
    {
        "inputs": [
            {"name": "_nodeId", "type": "string"},
            {"name": "_reason", "type": "string"}
        ],
        "name": "accuse",
        "outputs": [],
        "type": "function"
    },
    {
        "inputs": [{"name": "_nodeId", "type": "string"}],
        "name": "queryStatus",
        "outputs": [{"name": "status", "type": "string"}],
        "type": "function"
    },
    {
        "inputs": [{"name": "_nodeId", "type": "string"}],
        "name": "getAccusationCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "inputs": [],
        "name": "threshold",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
]

# 函数签名 -> selector (keccak256 前 4 字节)
# 预计算避免运行时依赖
FUNC_SELECTORS = {
    "accuse(string,string)":        "0xd1574845",
    "queryStatus(string)":          "0x3e9bb640",
    "getAccusationCount(string)":   "0x8f601f66",
    "threshold()":                  "0x42cde4e8",
}


def _encode_string_param(s: str) -> str:
    """ABI 编码一个 string 参数 (简化版)"""
    s_bytes = s.encode("utf-8")
    # offset (32 bytes) + length (32 bytes) + data (padded to 32)
    data_padded_len = ((len(s_bytes) + 31) // 32) * 32
    offset = 32  # 一个参数时 offset=0x20
    length = len(s_bytes)
    result = offset.to_bytes(32, "big").hex()
    result += length.to_bytes(32, "big").hex()
    result += s_bytes.hex().ljust(data_padded_len * 2, "0")
    return result


def _encode_two_strings(s1: str, s2: str) -> str:
    """ABI 编码两个 string 参数"""
    s1_bytes = s1.encode("utf-8")
    s2_bytes = s2.encode("utf-8")

    s1_padded = ((len(s1_bytes) + 31) // 32) * 32
    s2_padded = ((len(s2_bytes) + 31) // 32) * 32

    # 两个 offset (各 32 bytes)
    # s1 数据从 offset=64 开始
    s1_offset = 64
    # s2 数据从 s1 结束后开始
    s2_offset = s1_offset + 32 + s1_padded  # 32 for length field

    result = s1_offset.to_bytes(32, "big").hex()
    result += s2_offset.to_bytes(32, "big").hex()
    # s1
    result += len(s1_bytes).to_bytes(32, "big").hex()
    result += s1_bytes.hex().ljust(s1_padded * 2, "0")
    # s2
    result += len(s2_bytes).to_bytes(32, "big").hex()
    result += s2_bytes.hex().ljust(s2_padded * 2, "0")
    return result


class FiscoClient:
    """
    通过本地 FISCO-BCOS 轻节点 JSON-RPC 接口交互。

    轻节点进程由 entrypoint.py 在容器启动时拉起, 监听 127.0.0.1:20200。
    本客户端发 JSON-RPC 请求给轻节点, 轻节点再通过 P2P 与全节点通信。
    """

    def __init__(self, light_node_rpc: str = "http://127.0.0.1:20200",
                 group_id: int = 1, contract_address: str = ""):
        """
        :param light_node_rpc: 本地轻节点 JSON-RPC 地址
        :param group_id: FISCO group ID
        :param contract_address: 黑名单合约地址
        """
        self.rpc_url = light_node_rpc
        self.group_id = group_id
        self.contract_address = contract_address
        self._connected = False
        self._rpc_id = 0
        self._fisco_process_pid = None

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _rpc_call(self, method: str, params: list, timeout: float = 30.0) -> dict:
        """发送 JSON-RPC 请求到本地轻节点"""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._next_id()
        }
        resp = requests.post(self.rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def connect(self, max_retries: int = 10, retry_interval: float = 3.0):
        """
        检测本地轻节点是否已就绪 (JSON-RPC 可达)
        """
        for attempt in range(1, max_retries + 1):
            try:
                result = self._rpc_call("getClientVersion", [self.group_id])
                if "result" in result:
                    version_info = result["result"]
                    logger.info(f"connected to local light node: {version_info}")
                    self._connected = True

                    # 记录轻节点进程 PID (用于 metrics)
                    self._find_fisco_pid()
                    return
                elif "error" in result:
                    logger.warning(f"attempt {attempt}: RPC error: {result['error']}")
            except requests.exceptions.ConnectionError:
                logger.warning(f"attempt {attempt}/{max_retries}: light node not reachable yet")
            except Exception as e:
                logger.warning(f"attempt {attempt}/{max_retries}: {e}")

            if attempt < max_retries:
                time.sleep(retry_interval)

        logger.error("failed to connect to local light node, falling back to mock mode")
        self._connected = False

    def _find_fisco_pid(self):
        """查找 fisco-bcos 进程的 PID"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "fisco-bcos"],
                capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().split("\n")
            if pids and pids[0]:
                self._fisco_process_pid = int(pids[0])
                logger.info(f"fisco-bcos process PID: {self._fisco_process_pid}")
        except Exception:
            pass

    def is_connected(self) -> bool:
        return self._connected

    def get_fisco_pid(self) -> Optional[int]:
        return self._fisco_process_pid

    # ==================== 合约交互 ====================

    def accuse(self, node_id: str, reason: str) -> dict:
        """
        写入操作: 向链上黑名单合约提交指控交易

        通过轻节点 sendRawTransaction -> 全节点共识打包 -> 返回回执
        """
        start = time.time()

        if not self._connected:
            return self._mock_accuse(node_id, reason, start)

        try:
            # 编码交易数据: accuse(string,string)
            selector = FUNC_SELECTORS["accuse(string,string)"]
            encoded_params = _encode_two_strings(node_id, reason)
            data = selector + encoded_params

            # 通过轻节点 JSON-RPC 发送交易
            result = self._rpc_call("sendRawTransaction", [
                self.group_id,
                {
                    "to": self.contract_address,
                    "data": data,
                }
            ])

            latency_ms = (time.time() - start) * 1000

            if "result" in result:
                tx_result = result["result"]
                return {
                    "tx_hash": tx_result if isinstance(tx_result, str) else tx_result.get("transactionHash", ""),
                    "block_number": tx_result.get("blockNumber", 0) if isinstance(tx_result, dict) else 0,
                    "status": "success",
                    "latency_ms": round(latency_ms, 2)
                }
            else:
                return {
                    "error": result.get("error", {}).get("message", str(result)),
                    "status": "failed",
                    "latency_ms": round(latency_ms, 2)
                }
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"accuse transaction failed: {e}")
            return {"error": str(e), "latency_ms": round(latency_ms, 2)}

    def query_status(self, node_id: str) -> dict:
        """
        读取操作: 查询目标节点是否在链上黑名单中

        通过轻节点 call (只读, 不上链) 查询合约状态
        """
        start = time.time()

        if not self._connected:
            return self._mock_query(node_id, start)

        try:
            # 编码调用数据: queryStatus(string)
            selector = FUNC_SELECTORS["queryStatus(string)"]
            encoded_params = _encode_string_param(node_id)
            data = selector + encoded_params

            result = self._rpc_call("call", [
                self.group_id,
                {
                    "to": self.contract_address,
                    "data": data,
                }
            ])

            latency_ms = (time.time() - start) * 1000

            if "result" in result:
                output = result["result"]
                # 解码返回的 string
                status = self._decode_string_output(output)
                return {
                    "node_id": node_id,
                    "status": status,
                    "latency_ms": round(latency_ms, 2)
                }
            else:
                return {
                    "node_id": node_id,
                    "error": result.get("error", {}).get("message", str(result)),
                    "latency_ms": round(latency_ms, 2)
                }
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"query status failed: {e}")
            return {"node_id": node_id, "error": str(e), "latency_ms": round(latency_ms, 2)}

    def get_block_number(self) -> Optional[int]:
        """获取轻节点当前已同步的区块高度"""
        if not self._connected:
            return None
        try:
            result = self._rpc_call("getBlockNumber", [self.group_id])
            if "result" in result:
                bn = result["result"]
                return int(bn, 16) if isinstance(bn, str) and bn.startswith("0x") else int(bn)
            return None
        except Exception as e:
            logger.error(f"getBlockNumber failed: {e}")
            return None

    def get_sync_status(self) -> Optional[dict]:
        """获取轻节点同步状态"""
        if not self._connected:
            return None
        try:
            result = self._rpc_call("getSyncStatus", [self.group_id])
            return result.get("result")
        except Exception as e:
            logger.error(f"getSyncStatus failed: {e}")
            return None

    def get_peers(self) -> Optional[list]:
        """获取轻节点已连接的对端节点信息"""
        if not self._connected:
            return None
        try:
            result = self._rpc_call("getPeers", [self.group_id])
            return result.get("result")
        except Exception as e:
            logger.error(f"getPeers failed: {e}")
            return None

    def close(self):
        """清理资源"""
        pass

    # ==================== 辅助方法 ====================

    @staticmethod
    def _decode_string_output(hex_output) -> str:
        """从 ABI 编码的 hex 输出中解码 string"""
        if isinstance(hex_output, dict):
            hex_output = hex_output.get("output", "")
        if not hex_output or hex_output == "0x":
            return "ACTIVE"
        try:
            raw = hex_output.replace("0x", "")
            if len(raw) < 128:
                return "ACTIVE"
            # offset(32) + length(32) + data
            length = int(raw[64:128], 16)
            data_hex = raw[128:128 + length * 2]
            return bytes.fromhex(data_hex).decode("utf-8")
        except Exception:
            return "ACTIVE"

    # ==================== Mock 模式 ====================

    _mock_blacklist = set()

    def _mock_accuse(self, node_id: str, reason: str, start: float) -> dict:
        FiscoClient._mock_blacklist.add(node_id)
        latency_ms = (time.time() - start) * 1000
        logger.info(f"[MOCK] accuse {node_id}: {reason}")
        return {
            "tx_hash": f"0xmock_{node_id}_{int(time.time())}",
            "block_number": 0,
            "status": "success",
            "latency_ms": round(latency_ms, 2)
        }

    def _mock_query(self, node_id: str, start: float) -> dict:
        status = "BLOCKED" if node_id in FiscoClient._mock_blacklist else "ACTIVE"
        latency_ms = (time.time() - start) * 1000
        logger.info(f"[MOCK] query {node_id} => {status}")
        return {
            "node_id": node_id,
            "status": status,
            "latency_ms": round(latency_ms, 2)
        }
