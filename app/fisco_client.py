"""
FISCO-BCOS 轻节点客户端封装
通过 FISCO Python SDK (python_sdk) 与全节点通信，完成合约调用
"""
import json
import os
import time
import logging
from typing import Optional

from eth_utils import to_checksum_address

logger = logging.getLogger(__name__)

# 合约 ABI (编译后提取)
BLACKLIST_ABI = json.loads('''[
    {
        "inputs": [{"internalType": "uint256", "name": "_threshold", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "constructor"
    },
    {
        "inputs": [
            {"internalType": "string", "name": "_nodeId", "type": "string"},
            {"internalType": "string", "name": "_reason", "type": "string"}
        ],
        "name": "accuse",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "string", "name": "_nodeId", "type": "string"}],
        "name": "queryStatus",
        "outputs": [{"internalType": "string", "name": "status", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "string", "name": "_nodeId", "type": "string"}],
        "name": "getAccusationCount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "string", "name": "_nodeId", "type": "string"}],
        "name": "unblock",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "_newThreshold", "type": "uint256"}],
        "name": "setThreshold",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "threshold",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]''')


class FiscoClient:
    """
    封装 FISCO-BCOS 轻节点与全节点的通信逻辑。
    使用 FISCO Python SDK 的 BcosClient 进行合约交互。
    """

    def __init__(self, channel_host: str, channel_port: int, group_id: int,
                 contract_address: str, key_file: str = None):
        """
        :param channel_host: 全节点 channel 通信地址
        :param channel_port: 全节点 channel 端口
        :param group_id: FISCO group ID
        :param contract_address: 黑名单合约部署地址
        :param key_file: 客户端私钥文件路径
        """
        self.channel_host = channel_host
        self.channel_port = channel_port
        self.group_id = group_id
        self.contract_address = contract_address
        self.key_file = key_file or "/app/config/accounts/node_key.pem"
        self.client = None
        self._connected = False

    def connect(self, max_retries: int = 5, retry_interval: float = 3.0):
        """
        建立与 FISCO 全节点的连接，支持重试。
        """
        try:
            from client.bcosclient import BcosClient
            from client.datatype_parser import DatatypeParser

            # 配置 FISCO SDK client_config.py 通过环境变量覆盖
            os.environ.setdefault("BCOS_CHANNEL_HOST", self.channel_host)
            os.environ.setdefault("BCOS_CHANNEL_PORT", str(self.channel_port))
            os.environ.setdefault("BCOS_GROUP_ID", str(self.group_id))

            for attempt in range(1, max_retries + 1):
                try:
                    self.client = BcosClient()
                    info = self.client.getNodeVersion()
                    logger.info(f"connected to FISCO node: {info}")
                    self._connected = True
                    return
                except Exception as e:
                    logger.warning(f"connection attempt {attempt}/{max_retries} failed: {e}")
                    if attempt < max_retries:
                        time.sleep(retry_interval)
            logger.error("failed to connect to FISCO full node after retries")
        except ImportError:
            logger.warning("FISCO Python SDK not available, running in mock mode")
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def accuse(self, node_id: str, reason: str) -> dict:
        """
        向黑名单合约提交指控交易
        :return: {"tx_hash": str, "block_number": int, "latency_ms": float}
        """
        start = time.time()

        if not self._connected:
            return self._mock_accuse(node_id, reason, start)

        try:
            receipt = self.client.sendRawTransactionGetReceipt(
                to_address=self.contract_address,
                contract_abi=BLACKLIST_ABI,
                fn_name="accuse",
                args=[node_id, reason]
            )
            latency_ms = (time.time() - start) * 1000
            return {
                "tx_hash": receipt.get("transactionHash", ""),
                "block_number": int(receipt.get("blockNumber", "0x0"), 16),
                "status": "success" if receipt.get("status") == "0x0" else "failed",
                "latency_ms": round(latency_ms, 2)
            }
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"accuse transaction failed: {e}")
            return {"error": str(e), "latency_ms": round(latency_ms, 2)}

    def query_status(self, node_id: str) -> dict:
        """
        查询目标节点是否在黑名单中
        :return: {"node_id": str, "status": "ACTIVE"|"BLOCKED", "latency_ms": float}
        """
        start = time.time()

        if not self._connected:
            return self._mock_query(node_id, start)

        try:
            result = self.client.call(
                to_address=self.contract_address,
                contract_abi=BLACKLIST_ABI,
                fn_name="queryStatus",
                args=[node_id]
            )
            latency_ms = (time.time() - start) * 1000
            status = result[0] if isinstance(result, (list, tuple)) else str(result)
            return {
                "node_id": node_id,
                "status": status,
                "latency_ms": round(latency_ms, 2)
            }
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"query status failed: {e}")
            return {"node_id": node_id, "error": str(e), "latency_ms": round(latency_ms, 2)}

    def get_block_number(self) -> Optional[int]:
        """获取当前区块高度，用于同步延迟测量"""
        if not self._connected:
            return None
        try:
            return self.client.getBlockNumber()
        except Exception as e:
            logger.error(f"getBlockNumber failed: {e}")
            return None

    def close(self):
        if self.client:
            try:
                self.client.finish()
            except Exception:
                pass

    # ---------- mock methods for testing without FISCO ----------

    _mock_blacklist = set()

    def _mock_accuse(self, node_id: str, reason: str, start: float) -> dict:
        self._mock_blacklist.add(node_id)
        latency_ms = (time.time() - start) * 1000
        logger.info(f"[MOCK] accuse {node_id}: {reason}")
        return {
            "tx_hash": f"0xmock_{node_id}_{int(time.time())}",
            "block_number": 0,
            "status": "success",
            "latency_ms": round(latency_ms, 2)
        }

    def _mock_query(self, node_id: str, start: float) -> dict:
        status = "BLOCKED" if node_id in self._mock_blacklist else "ACTIVE"
        latency_ms = (time.time() - start) * 1000
        logger.info(f"[MOCK] query {node_id} => {status}")
        return {
            "node_id": node_id,
            "status": status,
            "latency_ms": round(latency_ms, 2)
        }
