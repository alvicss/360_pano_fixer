# 360 全景圖修復工具

這是一個用 Python 製作的 Windows 桌面工具，用來把一般圖片整理成較容易被平台辨識為 360 全景圖的 JPEG 檔案，並寫入 GPano XMP metadata。

## 功能

- 批次加入多張圖片或整個資料夾
- 自動檢查圖片是否接近 2:1 全景比例
- 可依設定自動裁切、跳過或保留原圖尺寸
- 自動轉成 JPEG，並補寫 360 全景 metadata
- 支援 GUI 操作，也保留簡單 CLI 模式

## 環境需求

- Windows
- Python 3.11 以上

## 本機執行

```powershell
Set-Location "專案根目錄"
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python pano_fixer.py
```

## 打包成 EXE

```powershell
Set-Location "專案根目錄"
. .\.venv\Scripts\Activate.ps1
python -m PyInstaller "全景圖修復工具.spec"
```

打包完成後，輸出檔會在：

```text
dist\全景圖修復工具.exe
```

## CLI 用法

```powershell
python pano_fixer.py --cli image1.jpg image2.png
```

## 版本控制與公開

- `.gitignore` 已排除虛擬環境、建置輸出、暫存檔與常見敏感檔案
- 文件內不再使用個人電腦的私人絕對路徑範例
- 建議只提交原始碼、`requirements.txt`、`spec`、文件與必要資產
