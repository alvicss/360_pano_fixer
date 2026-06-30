# 360 全景圖工具 - Windows 打包與使用教學

## 工具現在能做什麼？

1. 用桌面 UI 一次加入多張圖片或整個資料夾
2. 自動檢查圖片是不是 2:1 全景比例
3. 依預設值自動裁切、轉成 JPG、補上 GPano metadata
4. 輸出 `原檔名_360.jpg`
5. 也能在 UI 裡調整輸出品質、輸出位置、裁切偏移、進階 360 metadata

> 預設設定已經適合一般使用者，不懂 metadata 也可以直接按「開始批次處理」。

---

## 步驟一：安裝 Python

1. 到 https://www.python.org/downloads/ 下載最新版
2. 安裝時勾選 `Add Python to PATH`

確認安裝成功：

```powershell
python --version
```

---

## 步驟二：建立虛擬環境並安裝套件

```powershell
cd C:\Users\你的帳號\Desktop\pano-tool
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 步驟三：先直接執行

```powershell
python pano_fixer.py
```

會開啟桌面 UI。你可以：

- 按 `加入圖片` 選多張圖
- 按 `加入資料夾` 整批載入
- 用底部進度條看批次進度
- 在 `基本設定` / `進階 Metadata` 頁面中滑鼠停 1 秒看參數說明
- 保持預設值直接按 `開始批次處理`

### 預設值

- 非 2:1 圖片：自動裁切成 2:1
- 輸出位置：原圖旁邊
- JPEG 品質：95
- 背景色：`#FFFFFF`
- 進階 GPano 欄位：只寫安全常用值，其餘維持空白或自動計算

---

## 步驟四：打包成 exe

現在是 GUI 版，建議用 `--windowed`：

```powershell
pyinstaller --onefile --windowed --name "全景圖修復工具" pano_fixer.py
```

或直接使用專案內的 spec：

```powershell
pyinstaller "全景圖修復工具.spec"
```

打包完成後，exe 會出現在：

```text
dist\全景圖修復工具.exe
```

---

## 使用方式

### 方法 A：雙擊開啟

雙擊 `全景圖修復工具.exe`，在 UI 中加入圖片後批次處理。

### 方法 B：拖曳圖片到 exe

把一張或多張圖片拖到 exe 上，程式會開啟 UI 並自動把圖片加入清單，接著用目前設定開始處理。

### 方法 C：命令列模式

如果你之後要串自動化，也保留了 CLI 模式：

```powershell
python pano_fixer.py --cli a.png b.jpg c.webp
```

---

## UI 內可調整的項目

### 基本設定

- 非 2:1 圖片要自動裁切、跳過，或照原圖輸出
- 比例容差
- 裁切保留位置 X / Y
- 輸出到原圖旁，或輸出到指定資料夾
- 檔名後綴
- JPEG 品質
- 透明背景補色
- 透明背景補色可用 `選色` 按鈕挑色
- 是否覆蓋舊檔

### 進階 Metadata

- `ProjectionType`
- `UsePanoramaViewer`
- `SourcePhotosCount`
- `ExposureLockUsed`
- `PoseHeadingDegrees`
- `PosePitchDegrees`
- `PoseRollDegrees`
- `InitialViewHeadingDegrees`
- `InitialViewPitchDegrees`
- `InitialViewRollDegrees`
- `InitialHorizontalFOVDegrees`
- `InitialCameraDolly`
- `FullPanoWidthPixels`
- `FullPanoHeightPixels`
- `CroppedAreaLeftPixels`
- `CroppedAreaTopPixels`
- `LargestValidInteriorRectLeft`
- `LargestValidInteriorRectTop`
- `LargestValidInteriorRectWidth`
- `LargestValidInteriorRectHeight`

> 不確定要填什麼時，保持預設值即可。

### 其他操作

- 左側清單可看每張圖的成功 / 失敗狀態
- 處理完成後可雙擊該列直接開啟輸出檔
- 視窗縮小時會優先壓縮左側清單，右側設定欄會盡量保留可操作寬度

---

## 後續可再擴充的方向

- 預設設定檔存檔 / 載入
- 處理前預覽裁切範圍
- 成功 / 失敗結果匯出 CSV
- 多執行緒批次加速
