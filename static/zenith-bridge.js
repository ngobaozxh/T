#!/usr/bin/env node
/**
 * ZenithCore TCP bridge (Node.js, zero dependencies).
 *
 * Listens on a local TCP port and forwards all traffic over a WebSocket to a
 * port inside the ZenithCore container — lets you use a SOCKS5 / HTTP proxy
 * that only listens on loopback inside the container.
 *
 * Usage:
 *   node zenith-bridge.js <localPort> "wss://host/ws/tcp?port=1080" "<CONSOLE_TOKEN>"
 *
 * Requires Node.js 18+ (built-in WebSocket) or Node 21+.
 * On Node 18/20 start with:  node --experimental-websocket zenith-bridge.js ...
 */

'use strict';

const net = require('net');

const [, , localPortRaw, wsUrlRaw, token] = process.argv;

if (!localPortRaw || !wsUrlRaw) {
  console.error('Usage: node zenith-bridge.js <localPort> <wss://host/ws/tcp?port=N> [token]');
  process.exit(1);
}

if (typeof WebSocket === 'undefined') {
  console.error('WebSocket không khả dụng. Dùng Node.js 21+ hoặc chạy với --experimental-websocket.');
  process.exit(1);
}

const localPort = parseInt(localPortRaw, 10);
const url = new URL(wsUrlRaw);
if (token) url.searchParams.set('token', token);
const target = url.toString();

const server = net.createServer((socket) => {
  const ws = new WebSocket(target);
  ws.binaryType = 'arraybuffer';

  const pending = [];
  let open = false;

  ws.addEventListener('open', () => {
    open = true;
    while (pending.length) ws.send(pending.shift());
  });

  ws.addEventListener('message', (event) => {
    const data = event.data;
    if (typeof data === 'string') socket.write(Buffer.from(data, 'utf8'));
    else socket.write(Buffer.from(data));
  });

  ws.addEventListener('close', () => socket.destroy());
  ws.addEventListener('error', (err) => {
    console.error('[ws] lỗi:', err.message || err);
    socket.destroy();
  });

  socket.on('data', (chunk) => {
    if (open) ws.send(chunk);
    else pending.push(chunk);
  });
  socket.on('close', () => { try { ws.close(); } catch (_) {} });
  socket.on('error', () => { try { ws.close(); } catch (_) {} });
});

server.listen(localPort, '127.0.0.1', () => {
  console.log(`ZenithCore bridge  127.0.0.1:${localPort}  ->  ${url.origin}${url.pathname}`);
  console.log('Ctrl+C để dừng.');
});
