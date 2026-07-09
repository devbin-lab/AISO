# 번들 C/C++ 툴체인(w64devkit, 포터블 MinGW-w64) 설치 스크립트.
# tools/ 는 .gitignore 대상이라 클론 직후엔 없다. 이 스크립트로 내려받아 tools/w64devkit 에 푼다.
# run_code(C/C++ 검증)와 패키징(extraResources)이 이 경로를 사용한다.
#
# 사용:  powershell -ExecutionPolicy Bypass -File scripts/fetch-toolchain.ps1

$ErrorActionPreference = 'Stop'

$version = '2.8.0'
$url = "https://github.com/skeeto/w64devkit/releases/download/v$version/w64devkit-x64-$version.7z.exe"

$root = Split-Path -Parent $PSScriptRoot          # 프로젝트 루트
$tools = Join-Path $root 'tools'
$dest = Join-Path $tools 'w64devkit'
$sfx = Join-Path $tools 'w64devkit.7z.exe'

if (Test-Path (Join-Path $dest 'bin\g++.exe')) {
  Write-Host "이미 설치됨: $dest (건너뜀)"
  exit 0
}

New-Item -ItemType Directory -Force -Path $tools | Out-Null

Write-Host "다운로드: $url"
Invoke-WebRequest -Uri $url -OutFile $sfx

Write-Host "압축 해제 → $tools"
# w64devkit 배포본은 7-Zip 자동압축해제 실행 파일. -y -o 스위치로 무인 추출.
& $sfx -y "-o$tools" | Out-Null

Remove-Item $sfx -Force
if (Test-Path (Join-Path $dest 'bin\g++.exe')) {
  Write-Host "완료: $dest"
  & (Join-Path $dest 'bin\g++.exe') --version | Select-Object -First 1
} else {
  Write-Error "설치 실패: g++.exe 를 찾을 수 없습니다."
  exit 1
}
