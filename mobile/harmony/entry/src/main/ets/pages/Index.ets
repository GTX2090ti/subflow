import { webview } from '@kit.ArkWeb';
import { common, Want } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { util } from '@kit.ArkTS';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { Bridge } from '../model/Bridge';

const DOMAIN = 0x0000;
const TAG = 'SubFlow';

// 页面以虚拟域名加载，拿到标准 https origin（ES Module / localStorage 才能正常工作），
// 该域名下的请求全部由 onInterceptRequest 从 rawfile 回填，不联网。
const VHOST = 'subflow.local';
const ORIGIN = 'https://' + VHOST;
const ENTRY = ORIGIN + '/index.html';
// web 资源平铺在 resources/rawfile 根目录（sync-web.mjs 保证），所以前缀是空串。
const RAWFILE_ROOT = '';
const DEFAULT_SERVER = 'http://192.168.2.180:8099';

const INJECT = '<script>window.__SUBFLOW_APP__=1;'
  + 'window.__SUBFLOW_DEFAULT_SERVER__=' + JSON.stringify(DEFAULT_SERVER) + ';</script>';

@Entry
@Component
struct Index {
  private controller: webview.WebviewController = new webview.WebviewController();
  // 必须持有强引用：临时对象交给内核后会被 ArkTS GC 回收，
  // 之后页内调用会报 "native proxy object not found" 并拿到空返回。
  private bridge: Bridge = new Bridge();
  private dec: util.TextDecoder = util.TextDecoder.create('utf-8');
  @State loadError: string = '';

  private mimeOf(ext: string): string {
    switch (ext) {
      case 'html': return 'text/html';
      case 'js': return 'text/javascript';
      case 'css': return 'text/css';
      case 'json': return 'application/json';
      case 'svg': return 'image/svg+xml';
      case 'png': return 'image/png';
      case 'jpg': return 'image/jpeg';
      case 'jpeg': return 'image/jpeg';
      case 'ico': return 'image/x-icon';
      case 'webmanifest': return 'application/manifest+json';
      case 'woff': return 'font/woff';
      case 'woff2': return 'font/woff2';
      case 'ttf': return 'font/ttf';
      case 'txt': return 'text/plain';
      default: return 'application/octet-stream';
    }
  }

  private isTextExt(ext: string): boolean {
    return ext === 'css' || ext === 'js' || ext === 'json' || ext === 'svg'
      || ext === 'txt' || ext === 'webmanifest';
  }

  // ArkWeb 只设 setResponseData + setResponseMimeType 时，内核不会把主文档当 HTML，
  // 真机上会把整页源码当纯文本显示。必须同时给 header、encoding 与 MIME，且文本直接传 string。
  private textResponse(code: number, text: string, mime?: string): WebResourceResponse {
    const type = mime ? mime : 'text/plain';
    const resp = new WebResourceResponse();
    resp.setResponseHeader([{ headerKey: 'Content-Type', headerValue: type + '; charset=utf-8' }]);
    resp.setResponseData(text);
    resp.setResponseEncoding('utf-8');
    resp.setResponseMimeType(type);
    resp.setResponseCode(code);
    resp.setReasonMessage(code === 200 ? 'OK' : (code === 404 ? 'Not Found' : 'Error'));
    resp.setResponseIsReady(true);
    return resp;
  }

  private serve(url: string): WebResourceResponse | null {
    if (!url.startsWith(ORIGIN)) {
      return null;
    }
    let rel = url.substring(ORIGIN.length);
    const cut = rel.indexOf('?');
    if (cut >= 0) {
      rel = rel.substring(0, cut);
    }
    const hash = rel.indexOf('#');
    if (hash >= 0) {
      rel = rel.substring(0, hash);
    }
    while (rel.startsWith('/')) {
      rel = rel.substring(1);
    }
    if (rel.length === 0) {
      rel = 'index.html';
    }
    if (rel.indexOf('..') >= 0) {
      return this.textResponse(403, 'Forbidden');
    }
    const dot = rel.lastIndexOf('.');
    const ext = dot >= 0 ? rel.substring(dot + 1).toLowerCase() : '';

    const ctx = this.getUIContext().getHostContext();
    if (!ctx) {
      return this.textResponse(503, 'No UI context');
    }
    try {
      const bytes: Uint8Array = ctx.resourceManager.getRawFileContentSync(RAWFILE_ROOT + rel);
      const type = this.mimeOf(ext);
      if (ext === 'html') {
        const html = this.dec.decodeToString(bytes);
        if (html.indexOf('</head>') >= 0) {
          return this.textResponse(200, html.replace('</head>', INJECT + '</head>'), type);
        }
      }
      if (this.isTextExt(ext)) {
        return this.textResponse(200, this.dec.decodeToString(bytes), type);
      }
      const resp = new WebResourceResponse();
      resp.setResponseHeader([{ headerKey: 'Content-Type', headerValue: type }]);
      resp.setResponseData(bytes.buffer as ArrayBuffer);
      resp.setResponseMimeType(type);
      resp.setResponseCode(200);
      resp.setReasonMessage('OK');
      resp.setResponseIsReady(true);
      return resp;
    } catch (e) {
      const err = e as BusinessError;
      hilog.warn(DOMAIN, TAG, 'rawfile missing: %{public}s (%{public}s)', rel, err.message);
      // 绝不能 return null：null 表示放行给内核去联网解析虚拟域名，会把本地 404 放大成 ERR_FAILED。
      return this.textResponse(404, 'Not Found: ' + rel);
    }
  }

  private isInternalUrl(url: string): boolean {
    return url.startsWith(ORIGIN) || url.startsWith('about:') || url.startsWith('data:')
      || url.startsWith('blob:') || url.startsWith('javascript:');
  }

  private openExternally(url: string): void {
    const host = this.getUIContext().getHostContext();
    if (!host) {
      return;
    }
    const ctx = host as common.UIAbilityContext;
    const want: Want = { action: 'ohos.want.action.viewData', uri: url };
    ctx.startAbility(want).catch((e: BusinessError) => {
      hilog.error(DOMAIN, TAG, 'startAbility failed: %{public}s', e.message);
    });
  }

  aboutToAppear(): void {
    this.bridge.setController(this.controller);
  }

  build() {
    Stack({ alignContent: Alignment.Center }) {
      Web({ src: ENTRY, controller: this.controller })
        .width('100%')
        .height('100%')
        .backgroundColor('#070B14')
        .domStorageAccess(true)
        .onlineImageAccess(true)
        .javaScriptAccess(true)
        .mixedMode(MixedMode.All)
        .darkMode(WebDarkMode.On)
        .forceDarkAccess(false)
        .mediaPlayGestureAccess(false)
        // 走声明式 javaScriptProxy：对象随 Web 创建注册、每次页面加载自动注入。
        // 实测 registerJavaScriptProxy + refresh() 在真机上会留下
        // "native proxy object not found, name:SFB"，页内调用永远拿不到返回值。
        .javaScriptProxy({
          object: this.bridge,
          name: 'SFB',
          methodList: ['ping', 'request', 'discover'],
          controller: this.controller,
        })
        .onInterceptRequest((ev) => {
          if (!ev || !ev.request) {
            return null;
          }
          return this.serve(ev.request.getRequestUrl());
        })
        .onErrorReceive((ev) => {
          if (!ev || !ev.request) {
            return;
          }
          const url = ev.request.getRequestUrl();
          let main = false;
          try {
            main = ev.request.isMainFrame();
          } catch (e) {
            main = false;
          }
          // onErrorReceive 对每个子资源都会回调（图片/字体/流被中断），
          // 只有主文档失败才该整页报错，否则会把一个正常页面用错误卡盖住。
          if (!main && url !== ENTRY) {
            hilog.warn(DOMAIN, TAG, 'subresource failed: %{public}s', url);
            return;
          }
          this.loadError = '[' + ev.error.getErrorCode() + '] ' + ev.error.getErrorInfo();
        })
        .onPageEnd(() => {
          this.loadError = '';
        })
        .onConsole((ev) => {
          if (ev && ev.message) {
            hilog.info(DOMAIN, TAG, 'console: %{public}s', ev.message.getMessage());
          }
          return false;
        })
        .onLoadIntercept((ev) => {
          if (!ev || !ev.data) {
            return false;
          }
          const url = ev.data.getRequestUrl();
          if (this.isInternalUrl(url)) {
            return false;
          }
          // 容器里没有新标签页，交给系统浏览器。
          this.openExternally(url);
          return true;
        })

      if (this.loadError.length > 0) {
        Column({ space: 12 }) {
          Text('页面加载失败').fontSize(18).fontColor('#E6F1FF').fontWeight(FontWeight.Medium)
          Text(this.loadError).fontSize(13).fontColor('#8FA6C4').textAlign(TextAlign.Center)
          Button('重试').fontSize(15).height(40).backgroundColor('#22D3EE').fontColor('#04121A')
            .onClick(() => {
              this.loadError = '';
              this.controller.loadUrl(ENTRY);
            })
        }
        .width('86%')
        .padding(24)
        .borderRadius(16)
        .backgroundColor('#101A2E')
        .border({ width: 1, color: '#1B3A55' })
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#070B14')
  }
}
