# Grow Castle Auto cho LDPlayer

Tool Python đọc màn hình Grow Castle để đánh wave tiếp theo hoặc Replay, kích hoạt kỹ năng đã hồi và xem quảng cáo thưởng vàng. Bạn mở sẵn game. Với `--auto-captcha`, auto play thử giải CAPTCHA rồi tiếp tục khi nhận ra màn game đã trở lại.

## 1. Cài đặt

- **Windows** với Python 3.10 trở lên. Chế độ đọc video dùng Windows Media Foundation.
- **LDPlayer** cho chế độ chạy trực tiếp. Dùng màn hình ngang **1280 × 720 (16:9)** và bố cục game như trong `video2.mp4` để khớp với ảnh mẫu.
- **NumPy** và **Pillow** được liệt kê trong [requirements.txt](requirements.txt). Giữ [play_templates.npz](play_templates.npz) cùng thư mục với `autoplay.py` để tool tải các ảnh mẫu.

Mở PowerShell và chạy các lệnh sau. Thay `D:\growcastle` bằng thư mục dự án nếu bạn đặt ở nơi khác.

```powershell
Set-Location 'D:\growcastle'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Các lệnh bên dưới gọi Python trong môi trường `.venv` trực tiếp nên có thể chạy ngay trong PowerShell.

## 2. Chạy thử trên video

```powershell
.\.venv\Scripts\python.exe autoplay.py --video video2.mp4
.\.venv\Scripts\python.exe autoplay.py --video video2.mp4 --mode replay
.\.venv\Scripts\python.exe captcha.py video1.mp4
```

- `video2.mp4` có đoạn đánh wave, xem quảng cáo nhận vàng và chơi Replay. Auto play in các hành động dự kiến với tiền tố `PREVIEW`. Diễn biến vẫn theo video đã ghi.
- `video1.mp4` có ba lượt CAPTCHA. `captcha.py` in thời gian, vị trí cốc ban đầu, vị trí dự đoán cuối và số khung hình hỗ trợ dự đoán.

Chạy trên video chỉ cần các thư viện Python đã cài và bộ giải mã video của Windows.

## 3. Chạy trên LDPlayer

1. Bật **ADB Debugging** ở chế độ **local** trong cài đặt LDPlayer.
2. Mở Grow Castle và để game ở màn chính có nút **Battle** và **Replay**. Tự xử lý các hộp thoại đang che màn hình, chẳng hạn hộp vật phẩm **Staff**.
3. Kiểm tra ADB thấy máy ảo với trạng thái `device`.

```powershell
& 'D:\LDPlayer\LDPlayer9\adb.exe' devices -l
```

Tool mặc định dùng `D:\LDPlayer\LDPlayer9\ldconsole.exe`, tìm `adb.exe` trong cùng thư mục và chọn thiết bị `emulator-5554`. Nếu máy bạn khác, truyền đường dẫn thật qua `--ldconsole` và serial từ danh sách ADB qua `--serial`.

```powershell
# Xem tool sẽ bấm gì trong 15 giây
.\.venv\Scripts\python.exe autoplay.py --seconds 15

# Bấm thật, bắt đầu bằng wave mới
.\.venv\Scripts\python.exe autoplay.py --tap

# Bấm thật, bắt đầu bằng Replay 100%
.\.venv\Scripts\python.exe autoplay.py --mode replay --tap
```

Chỉ `--tap` mới gửi thao tác bấm. Mỗi thiết bị ADB có một phiên auto play dùng `--tap` tại một thời điểm. Trước mỗi lần bấm, tool kiểm tra Grow Castle là ứng dụng đang hiển thị trên thiết bị đã chọn. Dùng `Ctrl+C` để dừng hoặc thêm `--seconds 60` để giới hạn thời gian chạy trực tiếp.

## 4. Auto play hoạt động thế nào

| Tình huống | Hành động |
| --- | --- |
| Màn chính | Tool nhận diện các nút từ ảnh mẫu và bấm **Battle** hoặc **Replay** theo chế độ đang chạy. Trong menu Replay, tool chọn ô **100%**. |
| Đang đánh | Tool nhìn thanh hồi kỹ năng. Khi thanh xanh đầy, tool bấm đơn vị sở hữu một lần và chờ thanh rời trạng thái đầy trước khi bấm lại ở lần hồi tiếp theo. Tool theo dõi 12 thanh: 9 hero của Lightning Castle, cây thần và hai hàng người tuyết. |
| Hộp **Wave Skip** | Tool bấm **X** để đóng hộp và tiếp tục đánh. |
| **Victory** | Tool chờ về màn chính, ưu tiên hộp thưởng vàng nếu đã bật tự xem quảng cáo, rồi tiếp tục theo chế độ hiện tại. |
| HP lâu đài về **0** | Tool nhận diện số 0 cùng thanh HP cạn, chuyển từ wave sang Replay và giữ Replay đến khi kết thúc phiên chạy. |
| Wave **BOSS** | Màn boss thay dòng `WAVE n` bằng thanh `BOSS`, nên tool nhận diện trận đấu bằng thanh HP/MP cộng thanh tiến trình hoặc các thanh hồi kỹ năng, và vẫn bấm kỹ năng bình thường. |
| **CAPTCHA** | Tool tạm dừng đánh. Nếu bật `--auto-captcha`, tool tự bấm **Start**, dò cốc và thử tối đa ba cốc theo thứ tự tin cậy khi game báo sai. Khi cần giải tay, auto play chờ bạn xử lý rồi tiếp tục. |
| Màn hình chưa nhận diện | Tool chờ. Nếu đang có hộp thoại lạ, hãy tự xử lý để game quay lại màn đã biết. |

Tool lấy ảnh qua ADB, chuẩn hóa ảnh về 1280 × 720 để đối chiếu mẫu, rồi quy đổi tọa độ bấm theo kích thước ảnh thật.

### Nâng Lightning Castle bằng kim cương

Tính năng mặc định tắt. Thêm `--upgrade-tower` để bật cùng chế độ Wave hoặc Replay.
Giữ `tower_templates.npz` cùng thư mục với `tower_vision.py` để tool nhận diện các số và nút của tháp.

```powershell
# Xem trước các quyết định trên clip hướng dẫn
.\.venv\Scripts\python.exe autoplay.py --video video3.mp4 --upgrade-tower

# Chạy Wave và bật nâng tháp
.\.venv\Scripts\python.exe autoplay.py --tap --upgrade-tower

# Chạy Replay và bật nâng tháp
.\.venv\Scripts\python.exe autoplay.py --mode replay --tap --upgrade-tower
```

1. Khi thấy **ít nhất 90 kim cương** ở màn chính, tool bấm ô vuông ngay phía trên cúp vàng để hiện các tầng tháp. Sau đó tool chọn ô thứ hai tính từ dưới lên và xác nhận **Lightning Castle** có kỹ năng **Chain Lightning**.
2. Tool đọc cấp tháp và giá của nút **LV UP** thường. Giá hợp lệ là **2 hoặc 3 kim cương**, kiểm tra lại trước mỗi cú bấm.
3. Sau mỗi lần bấm, tool xác nhận cấp tăng đúng 1 và số dư giảm đúng giá vừa đọc. Một đợt hoàn tất sau **30 lần bấm và 30 cấp**. Chi phí là 60 kim cương nếu giá luôn là 2, hoặc 90 nếu giá luôn là 3.
4. Tool đóng menu, trở lại auto play và chờ số dư xuống dưới 90 rồi đạt lại mốc này để bắt đầu đợt mới. Nếu cấp, số dư hoặc giá không khớp, đợt dừng để bạn kiểm tra.

Chế độ bấm thật lưu tiến độ theo thiết bị trong `.tower-upgrade-*.json`. Khi mở lại, tool đối chiếu cấp và số dư với tiến độ đã ghi trước khi gửi cú bấm tiếp theo. Clip `video3.mp4` bắt đầu với 22 kim cương nên chế độ xem trước chưa kích hoạt một đợt nâng.

### Quảng cáo thưởng vàng

Tool mặc định tự xem quảng cáo thưởng vàng. Dùng `--ads` để bật hoặc `--no-ads` để bỏ qua **GET** và tiếp tục chơi. Cả hai tùy chọn dùng được với Wave, Replay, `--auto-captcha` và `--upgrade-tower`. Nếu bạn mở quảng cáo thủ công, tool vẫn chờ nút **X** để đóng rồi chơi tiếp.

```powershell
# Chạy Wave và tự xem quảng cáo
.\.venv\Scripts\python.exe autoplay.py --mode wave --tap --ads

# Chạy Wave và bỏ qua quảng cáo
.\.venv\Scripts\python.exe autoplay.py --mode wave --tap --no-ads
```

Khi bật tự xem quảng cáo, tool xử lý như sau.

1. Sau khi thắng, tool chờ hộp thưởng xuất hiện trước khi đánh tiếp. Tool nhận diện biểu tượng vàng và cả hai cách hiển thị nút **GET**, có hoặc không có dòng đếm ngược, rồi bấm nút một lần.
2. Nếu cửa sổ quảng cáo đang hiển thị trong LDPlayer, tool tiếp tục theo dõi kể cả khi bạn khởi động giữa lượt xem. Trong lúc đó, tool tạm dừng vào trận, dùng kỹ năng, giải CAPTCHA và nâng tháp.
3. Tool nhận các mẫu **Reward granted** có dấu **X** liền kề, gồm cả chữ trắng đi kèm dấu X đen trên nút tròn sáng. Tool cũng nhận diện mẫu hai thẻ **Learn More** và thẻ **Watch now** có nút đóng riêng. Một lượt thưởng có thể gồm nhiều màn kết thúc nối tiếp nhau.
4. Tool bấm **X** khi nhận diện được và thử lại sau ít nhất 1,5 giây nếu nút vẫn hiện. Tool thử tối đa sáu lần cho mỗi dạng màn kết thúc. Nếu quảng cáo đứng yên mà chưa đóng được, tool tự tìm dấu **X** ở cả bốn góc màn hình, không phụ thuộc vị trí hay màu nền, và lần lượt bấm các vị trí tìm thấy. Nhờ vậy nút **X** nằm chỗ khác hoặc có màu nền khác vẫn đóng được. Nếu cần đóng tay, tool chờ đến khi màn game hiển thị ổn định rồi tiếp tục. Game tự cộng vàng. Log `gold HUD changed` cho biết vùng hiển thị số vàng đã thay đổi sau khi quay về.

### Tự giải CAPTCHA trong auto play

```powershell
# Đánh wave mới và tự giải CAPTCHA
.\.venv\Scripts\python.exe autoplay.py --mode wave --tap --auto-captcha

# Replay và tự giải CAPTCHA
.\.venv\Scripts\python.exe autoplay.py --mode replay --tap --auto-captcha
```

Thêm `--upgrade-tower` vào một trong hai lệnh nếu muốn bật cả nâng tháp.

Tool ưu tiên chụp cửa sổ LDPlayer. Nếu cửa sổ bị che và tool chưa bấm **Start**, nó chuyển sang ghi hình 16 giây qua ADB. Tool xác nhận có kim cương giữa bàn cốc trước khi bấm **Start**, rồi ghi hình trọn lượt xáo cốc. Bản ghi chạy tiếp sau cú bấm đầu tiên, nên khi game báo **Incorrect** tool vẫn đọc được kim cương hiện ra và thử cốc tiếp theo. Tool thử tối đa ba cốc theo thứ tự tin cậy, bỏ qua thời gian chờ "Try again". Nếu lượt đó không ra kết quả, auto play tự thử lại CAPTCHA tối đa ba lượt, mỗi lượt cách nhau bốn giây. Nếu vẫn còn mở, bạn có thể giải tay và auto play sẽ tiếp tục khi nhận ra màn game đã trở lại. Bỏ `--auto-captcha` để dùng chế độ giải tay.

Với máy ảo khác, dùng `--index` để chọn đúng cửa sổ LDPlayer. Nếu đặt `--serial` riêng, chọn serial của cùng máy ảo đó.

Khi tool đã bắt đầu lượt mà chưa gửi được cú chọn cốc, bản ghi được lưu trong `logs` để chạy lại bằng `captcha.py`. Ghi hình ADB lưu clip MP4. Chụp cửa sổ lưu khung hình bàn cốc và thời gian trong file NPZ. Dùng đường dẫn bản ghi xuất hiện trong log làm đối số cho `captcha.py`.

### Bộ dò CAPTCHA độc lập

`captcha.py` tìm cốc ban đầu từ hướng viên kim cương di chuyển, so khớp hình cốc ở nhiều góc để phát hiện cốc xoay nhanh, rồi theo dõi các vị trí qua từng khung hình. Tool chỉ trả về tọa độ khi có đủ khung hình cùng hỗ trợ một vị trí cuối.

Tool xử lý cả hai kiểu xáo cốc của game: kiểu đổi chỗ trái phải và kiểu xoay tròn cả vòng. Kết quả in kèm danh sách `ranked` để bạn biết các cốc dự bị. Bản ghi không ra kết quả vẫn được giữ trong `logs`, và `captcha.py` in các cốc ứng viên thay vì bỏ trống.

```powershell
# Quan sát CAPTCHA trực tiếp từ cửa sổ LDPlayer số 0
.\.venv\Scripts\python.exe ldplayer_captcha.py --index 0

# Thử bấm đúng một lần rồi thoát, nếu xác minh được bàn cốc qua ADB
.\.venv\Scripts\python.exe ldplayer_captcha.py --index 0 --tap
```

Đưa cửa sổ LDPlayer lên trước và chạy bộ dò trước khi bấm **Start** trên CAPTCHA để tool thấy đoạn kim cương đi vào cốc. Chế độ quan sát đọc hình từ cửa sổ LDPlayer, còn `--tap` dùng thêm ADB để xác minh bàn cốc và gửi cú bấm.

## 5. Tình trạng kiểm thử

- Đã chạy mô phỏng các quyết định Battle, Replay, kỹ năng và quảng cáo trên `video2.mp4`, và phân tích ba lượt CAPTCHA trên `video1.mp4`.
- Đã kiểm tra màn boss bằng ảnh chụp thật: tool nhận ra trận boss và bấm kỹ năng. Đã kiểm tra lại ảnh chụp wave 1266 × 723 và hai bản ghi CAPTCHA lỗi trong `logs` (kiểu xoay tròn).
- Đã kiểm thử trực tiếp trên LDPlayer với **Battle**, **Replay 100%**, kích hoạt lại kỹ năng sau hồi chiêu và nhận vàng từ cả hai mẫu kết thúc quảng cáo.
- Đã giải một lượt CAPTCHA trực tiếp trên LDPlayer bằng cách phân tích lại các khung hình của lượt vừa chạy rồi bấm cốc dưới cùng. Tạm dừng CAPTCHA trong auto play đã được kiểm tra trên video.
- Luồng `--auto-captcha` đã được kiểm tra bằng khung hình từ lượt thật và ADB mô phỏng, gồm bấm **Start**, chọn một cốc và tiếp tục Replay sau khi CAPTCHA được giải.
- Nhánh tự chuyển sang Replay khi HP về 0 đã được thử với ảnh HP 0 mô phỏng. Phần nâng tháp được đối chiếu với `video3.mp4` và mô phỏng 30 lần bấm với cả hai mức giá.
