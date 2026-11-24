# Bản dùng thử công khai trên Vercel

Website mở trang lập thực đơn trực tiếp. Mỗi trình duyệt có một phiên riêng, được lưu tự động bằng cookie. Danh mục 75 món mẫu được đóng gói sẵn. Phiên thử lưu tối đa 7 ngày kể từ lần ghi gần nhất.

## Chạy thử trên máy Windows

Mở PowerShell tại thư mục này rồi chạy:

    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    .\.venv\Scripts\python.exe -m uvicorn vercel_app:app --host 127.0.0.1 --port 8010

Mở [trang dùng thử](http://127.0.0.1:8010). Kho phiên cục bộ nằm trong `.local-data`. Dùng cửa sổ ẩn danh hoặc hồ sơ trình duyệt khác để thử hai khách độc lập.

## Triển khai lên Vercel

1. Đưa bản này lên một nhánh riêng trong kho mã dùng thử. Điểm khởi chạy FastAPI đã được đặt trong `pyproject.toml`.
2. Kết nối cơ sở dữ liệu PostgreSQL riêng cho bản thử. Đặt chuỗi kết nối trong biến môi trường `DATABASE_URL` của dự án Vercel. Nếu dùng Neon trên Vercel, có thể dùng tích hợp Neon để cấp biến trực tiếp. Dùng cơ sở dữ liệu sạch; ứng dụng tự tạo bảng lưu phiên thử lần đầu.
3. Chọn nhánh chứa bản này trong **Settings → Environments → Production → Branch Tracking**, sau đó tạo bản triển khai từ nhánh ấy. Dùng nơi lưu trữ và gói Vercel theo mức chi phí đã chọn.
4. Sau khi triển khai, mở domain chính trong cửa sổ ẩn danh. Thử vào trang lập thực đơn, tạo thực đơn, mở lại và kiểm tra hai trình duyệt chỉ thấy dữ liệu của chính mình.

Cookie phiên cần được giữ để xem lại thực đơn trên cùng trình duyệt. Trình duyệt mới hoặc việc xóa cookie sẽ bắt đầu một phiên trống.
