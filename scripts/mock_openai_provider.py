from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time


def _draft_payload(schema_name: str) -> dict:
	if schema_name == "sales_order_draft":
		return {
			"customer_query": "合成客户", "transaction_date": None, "delivery_date": None,
			"default_sales_mode": "wholesale", "warehouse_query": None, "remarks": None,
			"items": [{
				"item_query": "合成商品", "qty": 2, "uom": "Nos",
				"price": None, "warehouse_query": None,
			}],
		}
	if schema_name == "purchase_order_draft":
		return {
			"supplier_query": "合成供应商", "transaction_date": None, "schedule_date": None,
			"default_purchase_mode": "wholesale", "warehouse_query": None, "currency": None,
			"supplier_ref": None, "remarks": None,
			"items": [{
				"item_query": "合成商品", "qty": 2, "uom": "Nos",
				"price": None, "warehouse_query": None,
			}],
		}
	return {
		"item_query": "合成商品", "warehouse_query": "合成仓库",
		"adjustment_type": "set_target", "quantity": 10, "uom": "Nos",
		"posting_date": "2026-07-15", "reason": "合成压测",
	}


class MockProviderHandler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"
	server_version = "MyAppSyntheticProvider/1.0"

	def log_message(self, _format: str, *_args) -> None:
		return

	def _read_json(self) -> dict:
		length = int(self.headers.get("Content-Length") or 0)
		return json.loads(self.rfile.read(length) or b"{}")

	def _send_json(self, status: int, payload: dict) -> None:
		body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def do_GET(self) -> None:
		if self.path == "/health":
			self._send_json(200, {"status": "ok"})
			return
		if self.path == "/v1/models":
			self._send_json(200, {"data": [
				{"id": "synthetic-chat"}, {"id": "synthetic-embedding"},
			]})
			return
		self._send_json(404, {"error": "not found"})

	def do_POST(self) -> None:
		payload = self._read_json()
		if self.path == "/v1/chat/completions":
			self._chat(payload)
			return
		if self.path == "/v1/embeddings":
			self._embeddings(payload)
			return
		self._send_json(404, {"error": "not found"})

	def _chat(self, payload: dict) -> None:
		delay = self.server.response_delay_ms / 1000
		if payload.get("stream"):
			self.send_response(200)
			self.send_header("Content-Type", "text/event-stream")
			self.send_header("Cache-Control", "no-cache")
			self.send_header("Connection", "close")
			self.end_headers()
			chunks = [
				{"model": "synthetic-chat", "choices": [{"delta": {"content": "合成"}}]},
				{"model": "synthetic-chat", "choices": [{"delta": {"content": "响应"}}]},
				{
					"model": "synthetic-chat", "choices": [],
					"usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
				},
			]
			try:
				for chunk in chunks:
					time.sleep(delay)
					line = f"data: {json.dumps(chunk, ensure_ascii=False, separators=(',', ':'))}\n\n"
					self.wfile.write(line.encode("utf-8"))
					self.wfile.flush()
				self.wfile.write(b"data: [DONE]\n\n")
				self.wfile.flush()
			except (BrokenPipeError, ConnectionResetError):
				return
			return

		time.sleep(delay)
		response_format = payload.get("response_format") or {}
		schema_name = ((response_format.get("json_schema") or {}).get("name") or "")
		content = json.dumps(_draft_payload(schema_name), ensure_ascii=False) if schema_name else "合成响应"
		self._send_json(200, {
			"model": "synthetic-chat",
			"choices": [{"message": {"role": "assistant", "content": content}}],
			"usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
		})

	def _embeddings(self, payload: dict) -> None:
		time.sleep(self.server.embedding_delay_ms / 1000)
		values = payload.get("input") or []
		if isinstance(values, str):
			values = [values]
		vector = [0.001] * self.server.vector_size
		self._send_json(200, {
			"model": "synthetic-embedding",
			"data": [
				{"object": "embedding", "index": index, "embedding": vector}
				for index, _value in enumerate(values)
			],
			"usage": {"prompt_tokens": len(values), "total_tokens": len(values)},
		})


def main() -> int:
	parser = argparse.ArgumentParser(description="Synthetic OpenAI-compatible provider for MyApp load tests.")
	parser.add_argument("--host", default="0.0.0.0")
	parser.add_argument("--port", type=int, default=4020)
	parser.add_argument("--response-delay-ms", type=int, default=40)
	parser.add_argument("--embedding-delay-ms", type=int, default=20)
	parser.add_argument("--vector-size", type=int, default=1024)
	args = parser.parse_args()
	server = ThreadingHTTPServer((args.host, args.port), MockProviderHandler)
	server.response_delay_ms = max(0, args.response_delay_ms)
	server.embedding_delay_ms = max(0, args.embedding_delay_ms)
	server.vector_size = max(1, args.vector_size)
	server.serve_forever()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
