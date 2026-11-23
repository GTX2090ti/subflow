import { AbilityConstant, ConfigurationConstant, UIAbility, Want } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { window } from '@kit.ArkUI';
import { webview } from '@kit.ArkWeb';

const DOMAIN = 0x0000;
const TAG = 'SubFlow';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    // 允许 Chrome DevTools 通过 hdc fport 连接页内上下文，排查桥/接口问题。
    webview.WebviewController.setWebDebuggingAccess(true);
    // 页面是深色主题，跟随系统深浅色会让状态栏/启动页反色。
    try {
      this.context.getApplicationContext().setColorMode(ConfigurationConstant.ColorMode.COLOR_MODE_DARK);
    } catch (e) {
      const err = e as BusinessError;
      hilog.warn(DOMAIN, TAG, 'setColorMode failed: %{public}s', err.message);
    }
    hilog.info(DOMAIN, TAG, '%{public}s', 'Ability onCreate');
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) {
        hilog.error(DOMAIN, TAG, 'loadContent failed: %{public}s', JSON.stringify(err));
        return;
      }
      const main = windowStage.getMainWindowSync();
      main.setWindowSystemBarProperties({
        statusBarColor: '#070B14',
        statusBarContentColor: '#E6F1FF',
        navigationBarColor: '#070B14',
        navigationBarContentColor: '#E6F1FF',
      }).catch((e: Error) => {
        hilog.error(DOMAIN, TAG, 'setWindowSystemBarProperties failed: %{public}s', e.message);
      });
    });
  }

  onWindowStageDestroy(): void {
    hilog.info(DOMAIN, TAG, '%{public}s', 'Ability onWindowStageDestroy');
  }

  onForeground(): void {
    hilog.info(DOMAIN, TAG, '%{public}s', 'Ability onForeground');
  }

  onBackground(): void {
    hilog.info(DOMAIN, TAG, '%{public}s', 'Ability onBackground');
  }
}
