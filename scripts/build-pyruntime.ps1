# 임베디드 Python 런타임 번들 생성 — 사용자 PC에 Python이 없어도 FastAPI 사이드카가 돈다.
# Playwright는 시스템 Edge(channel=msedge)를 쓰므로 브라우저 바이너리는 다운로드하지 않는다.
# 결과: <repo>/pyruntime/  (electron-builder가 resources/pyruntime 으로 복사)
$ErrorActionPreference = 'Stop'
$PyVer = if ($env:AISO_PY_VER) { $env:AISO_PY_VER } else { '3.12.10' }
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'pyruntime'
$req = Join-Path $root 'python\requirements.txt'

Write-Host "[pyruntime] Python $PyVer 임베디드 번들 → $out"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

# 1) 임베디드 Python 다운로드·해제
$zip = Join-Path $env:TEMP "py-embed-$PyVer.zip"
$url = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip"
Write-Host "[pyruntime] 다운로드: $url"
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $out -Force

# 2) ._pth 편집 — site-packages + 앱 코드 경로 활성화
#    (import site: pip/site-packages 인식 / ..\python: main.py 임포트 — 패키징 시 resources/python)
$pth = (Get-ChildItem $out -Filter 'python*._pth')[0].FullName
$lines = Get-Content $pth
$lines = $lines -replace '^\s*#\s*import\s+site', 'import site'
$lines += 'Lib\site-packages'
$lines += '..\python'
Set-Content -Path $pth -Value $lines -Encoding ASCII

# 3) pip 부트스트랩 (임베디드엔 ensurepip이 없어 get-pip 사용)
$py = Join-Path $out 'python.exe'
$getpip = Join-Path $env:TEMP 'get-pip.py'
Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip -UseBasicParsing
& $py $getpip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip 실패 (exit $LASTEXITCODE)" }

# 4) 런타임 의존성 설치
& $py -m pip install --no-warn-script-location --no-cache-dir -r $req
if ($LASTEXITCODE -ne 0) { throw "requirements 설치 실패 (exit $LASTEXITCODE)" }

# 5) 용량 절감 — __pycache__ 제거 (playwright 드라이버/브라우저는 시스템 Edge라 미포함)
Get-ChildItem $out -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("[pyruntime] 완료 — {0:N0} MB" -f $size)
