/**
 * A slow link between the browser and Ninaivu, for the tests and the bench
 * that are about a phone far from home (tools/bench_first_load.mjs,
 * tests/shell_cache_ui.mjs).
 *
 * A small TCP relay rather than DevTools throttling: DevTools does not slow
 * the service worker's own requests, and what the worker does on a slow link
 * is the point. Every connection shares one downlink and one uplink, so six
 * parallel requests split the bandwidth the way they do on a real phone, and
 * each new connection pays a round trip before its first byte, as TCP does.
 *
 *   const link = await slowLink(HOME, { rtt: 300, down: 250000, up: 94000 });
 *   page.goto(link.url) … link.cut() … link.restore() … link.close()
 *
 * *down* and *up* are bytes a second.
 */
import net from 'node:net';

/** One shared link: data waits its turn behind what went before it. */
class Pipe {
  constructor(rate, oneWay) { this.rate = rate; this.oneWay = oneWay; this.free = 0; this.bytes = 0; }
  /** How long until this chunk has arrived at the far end. */
  slot(length) {
    const now = Date.now();
    const start = Math.max(now, this.free);
    this.free = start + (length / this.rate) * 1000;
    this.bytes += length;
    return this.free + this.oneWay - now;
  }
}

export function slowLink(to, { rtt = 300, down = 250000, up = 94000 } = {}) {
  const target = new URL(to);
  const downlink = new Pipe(down, rtt / 2);
  const uplink = new Pipe(up, rtt / 2);
  let reachable = true;
  const sockets = new Set();
  const server = net.createServer((client) => {
    if (!reachable) { client.destroy(); return; }
    const upstream = net.connect(Number(target.port || 80), target.hostname);
    sockets.add(client); sockets.add(upstream);
    const send = (sock, chunk, pipe) => {
      setTimeout(() => { if (!sock.destroyed) sock.write(chunk); }, pipe.slot(chunk.length));
    };
    let open = false;
    const held = [];
    setTimeout(() => { open = true; held.splice(0).forEach((c) => send(upstream, c, uplink)); }, rtt);
    client.on('data', (c) => (open ? send(upstream, c, uplink) : held.push(c)));
    upstream.on('data', (c) => send(client, c, downlink));
    const close = () => {
      client.destroy(); upstream.destroy();
      sockets.delete(client); sockets.delete(upstream);
    };
    client.on('error', close); upstream.on('error', close);
    client.on('close', () => setTimeout(close, rtt));
    upstream.on('close', () => setTimeout(close, rtt));
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({
    url: `http://127.0.0.1:${server.address().port}`,
    down: downlink,
    up: uplink,
    cut() { reachable = false; sockets.forEach((s) => s.destroy()); },
    restore() { reachable = true; },
    close() { server.close(); sockets.forEach((s) => s.destroy()); },
  })));
}
