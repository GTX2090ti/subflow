import { connection, http } from '@kit.NetworkKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { webview } from '@kit.ArkWeb';
import { hilog } from '@kit.PerformanceAnalysisKit';

const DOMAIN = 0x0000;
const TAG = 'SubFlowBridge';

interface BridgeRequest {
  method?: string;
  url: string;
  headers?: Record<string, string>;
  body?: string;
  connectTimeout?: number;
  readTimeout?: number;
}

interface BridgeResult {
  status: number;
  body: string;
  error?: string;
}

interface FoundServer {
  url: string;
  version: string;
}

interface DiscoverResult {
  ok: boolean;
  servers?: FoundServer[];
  local?: string[];
  scanned?: number;
  elapsed_ms?: number;
  error?: string;
}

interface HealthResp {
  version?: string;
}

interface ProbeHit {
  code: number;
  version: string;
}

const DISCOVER_PORT = 8099;
const PROBE_CONCURRENCY = 32;

function toMethod(name: string): http.RequestMethod {
  switch (name) {
    case 'POST': return http.RequestMethod.POST;
    case 'PUT': return http.RequestMethod.PUT;
    case 'DELETE': return http.RequestMethod.DELETE;
    case 'HEAD': return http.RequestMethod.HEAD;
    case 'OPTIONS': return http.RequestMethod.OPTIONS;
    default: return http.RequestMethod.GET;
  }
}

/**
 * 页内 JS 的 HTTP 出口。ArkWeb 虚拟域到局域网 http 后端会被 CORS / mixed content 拦，
 * 而 @ohos.net.http 不受同源策略约束，所以所有 API 调用都从这里走。
 * 桥保持无状态：服务器地址、账号密码每次请求由页面带下来。
 *
 * 协议是「同步入队 + 回调回填」：request 立刻返回，HTTP 完成后用 runJavaScript
 * 调页面里的 window.__sfbResolve(reqId, json)。不用 asyncMethodList ——
 * 真机上异步代理会报 "native proxy object not found"，页内只能拿到 0 而不是 Promise。
 */
export class Bridge {
  private controller: webview.WebviewController | null = null;

  setController(controller: webview.WebviewController): void {
    this.controller = controller;
  }

  ping(): string {
    return JSON.stringify({ mode: 'native' });
  }

  request(reqJson: string, reqId: string): string {
    let req: BridgeRequest;
    try {
      req = JSON.parse(reqJson) as BridgeRequest;
    } catch (e) {
      this.reply(reqId, { status: 0, body: '', error: 'bad request json' });
      return 'queued';
    }
    if (!req.url) {
      this.reply(reqId, { status: 0, body: '', error: 'missing url' });
      return 'queued';
    }
    this.run(req, reqId);
    return 'queued';
  }

  private reply(reqId: string, result: BridgeResult): void {
    const controller = this.controller;
    if (!controller) {
      hilog.error(DOMAIN, TAG, '%{public}s', 'reply skipped: no controller');
      return;
    }
    const payload = JSON.stringify(result);
    const js = 'window.__sfbResolve&&window.__sfbResolve('
      + JSON.stringify(reqId) + ',' + JSON.stringify(payload) + ')';
    controller.runJavaScript(js).catch((e: BusinessError) => {
      hilog.error(DOMAIN, TAG, 'runJavaScript failed: %{public}s', e.message);
    });
  }

  /**
   * 局域网自动发现：扫描本机所在 /24 网段的 8099 端口，返回可用的 SubFlow 地址。
   * 同样走「同步入队 + 回调回填」，结果用 window.__sfbDiscoverResolve 送回页面。
   */
  discover(reqId: string): string {
    this.runDiscover(reqId);
    return 'queued';
  }

  private replyDiscover(reqId: string, result: DiscoverResult): void {
    const controller = this.controller;
    if (!controller) {
      hilog.error(DOMAIN, TAG, '%{public}s', 'discover reply skipped: no controller');
      return;
    }
    const payload = JSON.stringify(result);
    const js = 'window.__sfbDiscoverResolve&&window.__sfbDiscoverResolve('
      + JSON.stringify(reqId) + ',' + JSON.stringify(payload) + ')';
    controller.runJavaScript(js).catch((e: BusinessError) => {
      hilog.error(DOMAIN, TAG, 'discover runJavaScript failed: %{public}s', e.message);
    });
  }

  private localIPv4(): string[] {
    const out: string[] = [];
    try {
      const net = connection.getDefaultNetSync();
      if (!net || net.netId === 0) {
        return out;
      }
      const props = connection.getConnectionPropertiesSync(net);
      const links = props.linkAddresses || [];
      for (const link of links) {
        const addr = link.address && link.address.address ? link.address.address : '';
        if (!addr || addr === '127.0.0.1' || addr.indexOf('169.254.') === 0) {
          continue;
        }
        if (addr.indexOf(':') >= 0) {
          continue;   // 跳过 IPv6
        }
        if (addr.split('.').length === 4 && out.indexOf(addr) < 0) {
          out.push(addr);
        }
      }
    } catch (e) {
      const err = e as BusinessError;
      hilog.warn(DOMAIN, TAG, 'localIPv4 failed: %{public}s', err.message);
    }
    return out;
  }

  private async probeOnce(u: string): Promise<ProbeHit> {
    const client = http.createHttp();
    try {
      const resp = await client.request(u, {
        method: http.RequestMethod.GET,
        connectTimeout: 700,
        readTimeout: 2500,
        expectDataType: http.HttpDataType.STRING,
        usingCache: false,
      });
      const body = typeof resp.result === 'string' ? resp.result as string : '';
      let version = '';
      try {
        const parsed = JSON.parse(body) as HealthResp;
        version = parsed.version ? String(parsed.version) : '';
      } catch (e) {
        version = '';
      }
      return { code: resp.responseCode, version: version };
    } catch (e) {
      return { code: 0, version: '' };
    } finally {
      client.destroy();
    }
  }

  private async probe(ip: string): Promise<FoundServer | null> {
    const url = 'http://' + ip + ':' + DISCOVER_PORT;
    // /api/ping 是新加轻量探针；老服务返回 404 时再退回 /api/health。
    // 连不上（code 0）直接判负，不再补一次，避免整个网段扫描耗时翻倍。
    const fast = await this.probeOnce(url + '/api/ping');
    if (fast.code === 200) {
      return { url: url, version: fast.version };
    }
    if (fast.code !== 404) {
      return null;
    }
    const slow = await this.probeOnce(url + '/api/health');
    if (slow.code === 200) {
      return { url: url, version: slow.version };
    }
    return null;
  }

  private async runDiscover(reqId: string): Promise<void> {
    const started = Date.now();
    const locals = this.localIPv4();
    if (locals.length === 0) {
      this.replyDiscover(reqId, { ok: false, error: '未获取到局域网 IP，请确认已连接 Wi-Fi' });
      return;
    }

    const subnets: string[] = [];
    for (const ip of locals) {
      const parts = ip.split('.');
      if (parts.length !== 4) {
        continue;
      }
      const base = parts[0] + '.' + parts[1] + '.' + parts[2];
      if (subnets.indexOf(base) < 0) {
        subnets.push(base);
      }
    }

    const hosts: string[] = [];
    for (const base of subnets) {
      for (let i = 1; i <= 254; i++) {
        hosts.push(base + '.' + i);
      }
    }

    const found: FoundServer[] = [];
    let next = 0;
    const worker = async (): Promise<void> => {
      while (next < hosts.length) {
        const ip = hosts[next];
        next++;
        const hit = await this.probe(ip);
        if (hit) {
          found.push(hit);
        }
      }
    };
    const workers: Promise<void>[] = [];
    const count = Math.min(PROBE_CONCURRENCY, hosts.length);
    for (let i = 0; i < count; i++) {
      workers.push(worker());
    }
    await Promise.all(workers);

    hilog.info(DOMAIN, TAG, 'discover done: %{public}d hosts, %{public}d servers',
      hosts.length, found.length);
    this.replyDiscover(reqId, {
      ok: true,
      servers: found,
      local: locals,
      scanned: hosts.length,
      elapsed_ms: Date.now() - started,
    });
  }

  private async run(req: BridgeRequest, reqId: string): Promise<void> {
    const options: http.HttpRequestOptions = {
      method: toMethod((req.method || 'GET').toUpperCase()),
      connectTimeout: req.connectTimeout && req.connectTimeout > 0 ? req.connectTimeout : 8000,
      readTimeout: req.readTimeout && req.readTimeout > 0 ? req.readTimeout : 30000,
      expectDataType: http.HttpDataType.STRING,
      usingCache: false,
    };
    if (req.headers) {
      options.header = req.headers;
    }
    if (req.body) {
      options.extraData = req.body;
    }

    const client = http.createHttp();
    try {
      const resp = await client.request(req.url, options);
      const raw = resp.result;
      const body = typeof raw === 'string' ? raw : JSON.stringify(raw);
      this.reply(reqId, { status: resp.responseCode, body: body });
    } catch (e) {
      const err = e as BusinessError;
      hilog.error(DOMAIN, TAG, 'request failed: %{public}s %{public}s', req.url, err.message);
      // HTTP 层面的失败（连不上、证书、超时）走返回值，不抛给页面。
      this.reply(reqId, { status: 0, body: '', error: `${err.code}: ${err.message}` });
    } finally {
      client.destroy();
    }
  }
}
