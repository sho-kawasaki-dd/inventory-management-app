[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [string]$Suffix = '',

    [string]$IsccPath = (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
)

$ErrorActionPreference = 'Stop'
$script:ExpectedInnoSetupVersion = '6.7.3'
$script:ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Invoke-CheckedExternal {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = & $Executable @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $details = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "外部コマンドが終了コード $exitCode で失敗しました: $Executable $($Arguments -join ' ')`n$details"
    }

    return (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine)
}

function Get-InnoSetupVersion {
    param([Parameter(Mandatory = $true)][string]$CompilerPath)

    $compilerDirectory = (Resolve-Path (Split-Path -Parent $CompilerPath)).Path.TrimEnd('\')
    $uninstallPaths = @(
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )

    foreach ($uninstallPath in $uninstallPaths) {
        $entries = Get-ItemProperty $uninstallPath -ErrorAction SilentlyContinue
        foreach ($entry in $entries) {
            if (-not $entry.InstallLocation -or -not $entry.DisplayVersion) {
                continue
            }

            $registeredDirectory = [IO.Path]::GetFullPath($entry.InstallLocation).TrimEnd('\')
            if ($registeredDirectory -ieq $compilerDirectory -and $entry.DisplayVersion -match '^\d+\.\d+\.\d+$') {
                return $entry.DisplayVersion
            }
        }
    }

    $fileVersion = (Get-Item -LiteralPath $CompilerPath).VersionInfo.ProductVersion
    if ($fileVersion -match '(?<!\d)(\d+\.\d+\.\d+)(?!\d)' -and $Matches[1] -ne '0.0.0') {
        return $Matches[1]
    }

    throw "ISCC の実版を特定できません。インストール登録または製品版情報を確認してください: $CompilerPath"
}

function Copy-SourceTree {
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][bool]$IncludeDependencySources,
        [Parameter(Mandatory = $true)][object]$Dependencies
    )

    $directories = @('src', 'scripts', 'tests', 'docs', 'packaging', '.github/workflows')
    $rootFiles = @('README.md', 'LICENSE', 'pyproject.toml', 'uv.lock', '.python-version')
    $excludedFilePattern = '(^|[\\/])(?:\.env(?:\.[^\\/]*)?|[^\\/]*\.(?:db|sqlite(?:3)?|log|pem|key))$'
    $excludedDirectoryPattern = '(^|[\\/])(?:\.git|\.venv|__pycache__|\.pytest_cache|\.ruff_cache|\.pyright|dist|build)([\\/]|$)'

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($relativeDirectory in $directories) {
        $sourceDirectory = Join-Path $script:ProjectRoot $relativeDirectory
        if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) {
            continue
        }

        Get-ChildItem -LiteralPath $sourceDirectory -File -Recurse -Force | ForEach-Object {
            $relativePath = [IO.Path]::GetRelativePath($script:ProjectRoot, $_.FullName)
            if ($relativePath -match $excludedFilePattern -or $relativePath -match $excludedDirectoryPattern) {
                return
            }

            $targetPath = Join-Path $Destination $relativePath
            New-Item -ItemType Directory -Path (Split-Path -Parent $targetPath) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $targetPath
        }
    }

    foreach ($relativeFile in $rootFiles) {
        $sourceFile = Join-Path $script:ProjectRoot $relativeFile
        if (Test-Path -LiteralPath $sourceFile -PathType Leaf) {
            Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $Destination $relativeFile)
        }
    }

    $dependencySourceDirectory = Join-Path $script:ProjectRoot 'packaging/dependency-sources'
    $dependencyFiles = @()
    if (Test-Path -LiteralPath $dependencySourceDirectory -PathType Container) {
        $dependencyFiles = @(Get-ChildItem -LiteralPath $dependencySourceDirectory -File -Recurse -Force)
    }
    if ($IncludeDependencySources -and $dependencyFiles.Count -eq 0) {
        throw 'リリースビルドには packaging/dependency-sources/ に版対応済みの依存ソースが必要です。'
    }

    $dependencyArtifacts = @()
    $dependencyIndexPath = Join-Path $dependencySourceDirectory 'manifest.json'
    if ($dependencyFiles.Count -gt 0 -and (Test-Path -LiteralPath $dependencyIndexPath -PathType Leaf)) {
        $dependencyIndex = Get-Content -LiteralPath $dependencyIndexPath -Raw | ConvertFrom-Json
        if ($dependencyIndex.schema_version -ne 1) {
            throw '依存ソース manifest.json の schema_version は 1 である必要があります。'
        }
        if ($IncludeDependencySources -and $dependencyIndex.audit_status -ne 'complete') {
            throw 'リリースビルドの依存ソース manifest.json は audit_status=complete が必要です。'
        }

        $seenComponents = @{}
        foreach ($source in @($dependencyIndex.sources)) {
            $component = [string]$source.component
            $sourceVersion = [string]$source.version
            $relativeSourcePath = [string]$source.path
            $expectedHash = [string]$source.sha256
            if ([string]::IsNullOrWhiteSpace($component) -or [string]::IsNullOrWhiteSpace($sourceVersion) -or
                [string]::IsNullOrWhiteSpace($relativeSourcePath) -or $expectedHash -notmatch '^[0-9a-fA-F]{64}$') {
                throw '依存ソース manifest.json の sources 項目には component/version/path/sha256 が必要です。'
            }
            if ($seenComponents.ContainsKey($component)) {
                throw "依存ソース manifest.json に component の重複があります: $component"
            }
            $seenComponents[$component] = $true

            if ([IO.Path]::IsPathRooted($relativeSourcePath) -or $relativeSourcePath -match '(^|[\\/])\.\.([\\/]|$)') {
                throw "依存ソースの path は依存ソース置き場からの相対パスにしてください: $relativeSourcePath"
            }
            $sourcePath = [IO.Path]::GetFullPath((Join-Path $dependencySourceDirectory $relativeSourcePath))
            $sourceRootPrefix = $dependencySourceDirectory.TrimEnd('\') + '\'
            if (-not $sourcePath.StartsWith($sourceRootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
                -not (Test-Path -LiteralPath $sourcePath -PathType Leaf) -or (Get-Item -LiteralPath $sourcePath).Length -eq 0) {
                throw "依存ソース manifest.json が参照するファイルがありません: $relativeSourcePath"
            }

            $actualHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash
            if ($actualHash -ine $expectedHash) {
                throw "依存ソースの SHA256 が一致しません: $relativeSourcePath"
            }
            $dependencyProperty = $Dependencies.PSObject.Properties | Where-Object Name -IEQ $component | Select-Object -First 1
            if ($dependencyProperty -and [string]$dependencyProperty.Value -ne $sourceVersion) {
                throw "依存ソースの版がビルド環境と一致しません: $component (source=$sourceVersion, build=$($dependencyProperty.Value))"
            }

            $dependencyArtifacts += [ordered]@{
                component = $component
                version = $sourceVersion
                path = "packaging/dependency-sources/$relativeSourcePath"
                sha256 = $actualHash.ToLowerInvariant()
            }
        }
    }
    elseif ($IncludeDependencySources) {
        throw 'リリースビルドには packaging/dependency-sources/manifest.json が必要です。'
    }
    if ($IncludeDependencySources -and $dependencyArtifacts.Count -eq 0) {
        throw 'リリースビルドの依存ソース manifest.json に検証可能な sources がありません。'
    }

    $sensitiveFiles = @(Get-ChildItem -LiteralPath $Destination -File -Recurse -Force | Where-Object {
        $_.Name -match '^(?:\.env(?:\..*)?|.*\.(?:db|sqlite(?:3)?|log|pem|key))$'
    })
    if ($sensitiveFiles.Count -gt 0) {
        throw "ソース成果物に除外対象のファイルが含まれています: $($sensitiveFiles.FullName -join ', ')"
    }

    return [pscustomobject]@{
        included = $dependencyFiles.Count -gt 0
        indexed = $dependencyArtifacts.Count -gt 0
        artifacts = @($dependencyArtifacts)
    }
}

function Write-Utf8NoBom {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Text)

    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Write-Sha256File {
    param([Parameter(Mandatory = $true)][string]$Path)

    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    $hashPath = "$Path.sha256"
    $hashLine = "$hash  $([IO.Path]::GetFileName($Path))$([Environment]::NewLine)"
    Write-Utf8NoBom -Path $hashPath -Text $hashLine
}

Push-Location $script:ProjectRoot
try {
    $uvCommand = Get-Command uv -ErrorAction Stop
    $isReleaseBuild = $env:GITHUB_ACTIONS -eq 'true'
    # 対応ソース(-sources.zip)を展開したディレクトリには .git がない。Release ビルドでは git を必須とする
    $hasGitRepository = Test-Path -LiteralPath (Join-Path $script:ProjectRoot '.git')
    if ($isReleaseBuild -and -not $hasGitRepository) {
        throw 'Release ビルドには git リポジトリが必要です。'
    }
    $gitCommand = if ($hasGitRepository) { Get-Command git -ErrorAction Stop } else { $null }
    $resolvedIsccPath = (Resolve-Path -LiteralPath $IsccPath -ErrorAction Stop).Path
    $innoVersion = Get-InnoSetupVersion -CompilerPath $resolvedIsccPath
    if ($innoVersion -ne $script:ExpectedInnoSetupVersion) {
        throw "Inno Setup の版が一致しません。期待値=$($script:ExpectedInnoSetupVersion)、実際=$innoVersion、ISCC=$resolvedIsccPath"
    }

    $expectedTag = "v$Version$Suffix"
    $commitSha = 'unknown (git リポジトリなし)'
    if ($hasGitRepository) {
        $commitSha = (Invoke-CheckedExternal -Executable $gitCommand.Source -Arguments @('rev-parse', 'HEAD')).Trim()
    }
    $tag = $null
    if ($isReleaseBuild) {
        if ($env:GITHUB_REF_TYPE -ne 'tag' -or $env:GITHUB_REF_NAME -ne $expectedTag) {
            throw "CI のタグとビルド引数が一致しません。期待値=$expectedTag、実際=$env:GITHUB_REF_NAME"
        }

        $tagCommit = Invoke-CheckedExternal -Executable $gitCommand.Source -Arguments @('rev-parse', "$expectedTag^{commit}")
        if ($tagCommit.Trim() -ne $commitSha -or $env:GITHUB_SHA -ne $commitSha) {
            throw 'チェックアウトされたコミットがリリースタグのコミットと一致しません。'
        }
        $workingTree = Invoke-CheckedExternal -Executable $gitCommand.Source -Arguments @('status', '--porcelain', '--untracked-files=all')
        if (-not [string]::IsNullOrWhiteSpace($workingTree)) {
            throw "リリースビルドの作業ツリーに変更があります:`n$workingTree"
        }
        $tag = $expectedTag
    }

    $syncOutput = Invoke-CheckedExternal -Executable $uvCommand.Source -Arguments @('sync', '--locked')
    if ($syncOutput) { Write-Host $syncOutput }
    $versionOutput = Invoke-CheckedExternal -Executable $uvCommand.Source -Arguments @(
        'run', 'python', 'scripts/check_release_version.py', '--version', $Version, "--suffix=$Suffix"
    )
    $releaseVersion = $versionOutput | ConvertFrom-Json
    if ($releaseVersion.version -ne $Version -or $releaseVersion.suffix -ne $Suffix) {
        throw '版数検査スクリプトの JSON 出力が入力値と一致しません。'
    }

    $buildId = '{0}-{1}' -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'), ([Guid]::NewGuid().ToString('N').Substring(0, 8))
    $stagingDirectory = Join-Path $script:ProjectRoot "dist/staging/$buildId"
    $workDirectory = Join-Path $script:ProjectRoot "build/$buildId"
    $bundleDirectory = Join-Path $stagingDirectory 'inventory-manager-mini'
    $releaseDirectory = Join-Path $script:ProjectRoot "dist/releases/$buildId"
    New-Item -ItemType Directory -Path $stagingDirectory, $workDirectory -Force | Out-Null

    $pyInstallerOutput = Invoke-CheckedExternal -Executable $uvCommand.Source -Arguments @(
        'run', 'pyinstaller', '--noconfirm', '--clean',
        '--distpath', $stagingDirectory,
        '--workpath', $workDirectory,
        'packaging/inventory-manager-mini.spec'
    )
    if ($pyInstallerOutput) { Write-Host $pyInstallerOutput }
    if (-not (Test-Path -LiteralPath $bundleDirectory -PathType Container)) {
        throw "PyInstaller の onedir 出力がありません: $bundleDirectory"
    }

    $bundleDocuments = @(
        @{ Source = 'LICENSE'; Name = 'LICENSE' },
        @{ Source = 'packaging/DISTRIBUTION_LICENSE.txt'; Name = 'DISTRIBUTION_LICENSE.txt' },
        @{ Source = 'packaging/THIRD_PARTY_NOTICES.txt'; Name = 'THIRD_PARTY_NOTICES.txt' },
        @{ Source = 'packaging/SOURCE_CODE.txt'; Name = 'SOURCE_CODE.txt' }
    )
    foreach ($document in $bundleDocuments) {
        $sourceDocument = Join-Path $script:ProjectRoot $document.Source
        if (-not (Test-Path -LiteralPath $sourceDocument -PathType Leaf) -or (Get-Item $sourceDocument).Length -eq 0) {
            throw "同梱必須文書が存在しないか空です: $sourceDocument"
        }
        Copy-Item -LiteralPath $sourceDocument -Destination (Join-Path $bundleDirectory $document.Name)
    }

    $licenseDirectory = Join-Path $script:ProjectRoot 'packaging/licenses'
    if (-not (Test-Path -LiteralPath $licenseDirectory -PathType Container)) {
        throw "ライセンス本文のディレクトリがありません: $licenseDirectory"
    }
    Copy-Item -LiteralPath $licenseDirectory -Destination $bundleDirectory -Recurse

    $bundleCheckOutput = Invoke-CheckedExternal -Executable $uvCommand.Source -Arguments @(
        'run', 'python', 'scripts/check_bundle.py', $bundleDirectory
    )
    if ($bundleCheckOutput) { Write-Host $bundleCheckOutput }

    $isccOutputName = "inventory-manager-mini-$Version$Suffix-setup"
    $isccOutput = Invoke-CheckedExternal -Executable $resolvedIsccPath -Arguments @(
        "/DAppVersion=$Version",
        "/DOutputName=$isccOutputName",
        "/DBundleDir=$bundleDirectory",
        "/O$stagingDirectory",
        'packaging/inventory-manager-mini.iss'
    )
    if ($isccOutput) { Write-Host $isccOutput }
    $installerPath = Join-Path $stagingDirectory "$isccOutputName.exe"
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf) -or (Get-Item $installerPath).Length -eq 0) {
        throw "Inno Setup のインストーラー出力がありません: $installerPath"
    }

    $toolchainOutput = Invoke-CheckedExternal -Executable $uvCommand.Source -Arguments @(
        'run', 'python', '-c', @'
import importlib.metadata, json, platform, shutil, sys
import PyInstaller, PySide6
from PySide6 import QtCore
from PySide6.QtCore import qVersion
print(json.dumps({
    "python": platform.python_version(),
    "python_path": sys.executable,
    "pyinstaller": PyInstaller.__version__,
    "pyinstaller_path": shutil.which("pyinstaller") or PyInstaller.__file__,
    "qt": qVersion(),
    "qt_path": QtCore.__file__,
    "pyside6": PySide6.__version__,
    "pyside6_path": PySide6.__file__,
    "dependencies": {
        name: importlib.metadata.version(name)
        for name in ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6", "platformdirs")
    },
}))
'@
    )
    $toolchain = $toolchainOutput | ConvertFrom-Json
    $uvVersionOutput = Invoke-CheckedExternal -Executable $uvCommand.Source -Arguments @('--version')
    if ($uvVersionOutput -notmatch '^uv\s+(\d+\.\d+\.\d+\S*)') {
        throw "uv の版を特定できません: $uvVersionOutput"
    }
    $uvVersion = $Matches[1]

    $sourceName = "inventory-manager-mini-$Version$Suffix-sources.zip"
    $sourceTree = Join-Path $stagingDirectory 'source-tree'
    $dependencySourceInfo = Copy-SourceTree -Destination $sourceTree -IncludeDependencySources $isReleaseBuild -Dependencies $toolchain.dependencies
    $hasDependencySources = $dependencySourceInfo.included
    $sourceArchive = Join-Path $stagingDirectory $sourceName
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $sourceTree,
        $sourceArchive,
        [IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    Remove-Item -LiteralPath $sourceTree -Recurse -Force
    if ((Get-Item -LiteralPath $sourceArchive).Length -eq 0) {
        throw "ソース成果物が空です: $sourceArchive"
    }

    $manifest = [ordered]@{
        format_version = 1
        version = $Version
        suffix = $Suffix
        is_prerelease = [bool]$releaseVersion.is_prerelease
        tag = $tag
        commit_sha = $commitSha
        build_id = $buildId
        build_type = if ($isReleaseBuild) { 'release' } else { 'local-verification' }
        artifacts = @((Split-Path -Leaf $installerPath), $sourceName, "inventory-manager-mini-$Version$Suffix-manifest.json")
        dependency_sources_included = $hasDependencySources
        dependency_source_indexed = $dependencySourceInfo.indexed
        dependency_sources_present_for_release = [bool]($isReleaseBuild -and $hasDependencySources)
        dependencies = $toolchain.dependencies
        build_tools = [ordered]@{
            python = [ordered]@{
                version = $toolchain.python
                path = $toolchain.python_path
            }
            pyinstaller = [ordered]@{
                version = $toolchain.pyinstaller
                path = $toolchain.pyinstaller_path
            }
            qt = [ordered]@{
                version = $toolchain.qt
                path = $toolchain.qt_path
            }
            pyside6 = [ordered]@{
                version = $toolchain.pyside6
                path = $toolchain.pyside6_path
            }
            uv = [ordered]@{
                version = $uvVersion
                path = $uvCommand.Source
            }
            inno_setup = [ordered]@{
                version = $innoVersion
                path = $resolvedIsccPath
            }
        }
        source_mapping = @{
            application_sources = $sourceName
            dependency_sources = $dependencySourceInfo.artifacts
            dependency_source_status = if ($dependencySourceInfo.indexed) { 'index-and-hashes-checked' } elseif ($hasDependencySources) { 'unindexed-local-input' } else { 'not-included-local-verification' }
        }
    }
    $manifestPath = Join-Path $stagingDirectory "inventory-manager-mini-$Version$Suffix-manifest.json"
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    Write-Utf8NoBom -Path $manifestPath -Text ($manifestJson + [Environment]::NewLine)

    $distributionFiles = @($installerPath, $sourceArchive, $manifestPath)
    foreach ($file in $distributionFiles) {
        if ((Get-Item -LiteralPath $file).Length -eq 0) {
            throw "配布ファイルが空です: $file"
        }
        Write-Sha256File -Path $file
    }

    $candidateDirectory = Join-Path $stagingDirectory 'release-output'
    New-Item -ItemType Directory -Path $candidateDirectory -Force | Out-Null
    foreach ($file in $distributionFiles) {
        Copy-Item -LiteralPath $file -Destination $candidateDirectory
        Copy-Item -LiteralPath "$file.sha256" -Destination $candidateDirectory
    }
    $releaseParent = Split-Path -Parent $releaseDirectory
    New-Item -ItemType Directory -Path $releaseParent -Force | Out-Null
    if (Test-Path -LiteralPath $releaseDirectory) {
        throw "成果物の確定先が既に存在します: $releaseDirectory"
    }
    Move-Item -LiteralPath $candidateDirectory -Destination $releaseDirectory

    Write-Host "ビルドID: $buildId"
    Write-Host "ISCC: $resolvedIsccPath ($innoVersion)"
    Write-Host "ソース成果物: $sourceName"
    Write-Output $releaseDirectory
}
finally {
    Pop-Location
}