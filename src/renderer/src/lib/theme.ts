import type { ThemeMode } from '../../../shared/settings'

/** 테마를 <html data-theme="..."> 에 적용하고, 네이티브 타이틀바 색도 동기화한다. */
export function applyTheme(theme: ThemeMode): void {
  const resolved =
    theme === 'system'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light'
      : theme
  document.documentElement.dataset.theme = resolved
  // Electron 밖(일반 브라우저 미리보기)에서는 api가 없으므로 조용히 건너뜀
  window.api?.setWindowTheme?.(resolved)?.catch(() => {})
}
