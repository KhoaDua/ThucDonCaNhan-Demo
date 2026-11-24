# Bản dùng thử công khai trên Vercel

Khách mở link để đi thẳng đến checklist, xem lại ràng buộc và lập thực đơn. Trang kết quả hiển thị món, khẩu phần, dinh dưỡng theo ngày và danh sách mua sắm. Danh mục món mẫu được đóng gói sẵn.

Hồ sơ và thực đơn chỉ được xử lý trong bộ nhớ của từng yêu cầu. Biểu mẫu xem lại chuyển các câu trả lời sang bước xác nhận. Khi rời trang kết quả, khách có thể bắt đầu lượt thử mới từ checklist.

Checklist mới đưa sẵn các mức khẩu phần `1; 1,5; 2; 2,5` để khách xem lại và chỉnh trước khi lập thực đơn. Nếu các món được xét không đủ để đạt giới hạn năng lượng, nút **Sửa khẩu phần** mở lại đúng ô cần chỉnh và giữ các câu trả lời của lượt đó.

## Chạy thử trên máy Windows

Mở PowerShell tại thư mục này rồi chạy:

    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    .\.venv\Scripts\python.exe -m uvicorn vercel_app:app --host 127.0.0.1 --port 8010

Mở [trang dùng thử](http://127.0.0.1:8010).

## Triển khai lên Vercel

1. Đẩy mã lên nhánh `main` của kho dùng thử đã kết nối với Vercel. Cấu hình FastAPI và điểm khởi chạy đã có sẵn trong kho.
2. Chờ bản triển khai Production chuyển sang Ready. Ứng dụng chạy với danh mục mẫu có sẵn và không cần Neon hay biến `DATABASE_URL`.
3. Mở domain Production trong cửa sổ ẩn danh, chọn nhu cầu, xem lại ràng buộc và xác nhận tạo thực đơn.
