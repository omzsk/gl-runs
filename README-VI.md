# GitLab Full Restore Test — xử lý Wiki không tồn tại / lỗi xác thực

Bản cập nhật dành cho `gl-runs-main-no-artifacts.zip`. Chỉ cần **thay 2 file**:

- `scripts/verify_restore_full.py`
- `scripts/restore_summary_jobs.py`

Giữ nguyên `.github/workflows/restore-test.yml`, `.github/workflows/sync.yml`, `repos`, secrets và variables. Bản này chỉ **đọc** GitHub/GitLab, không sửa dữ liệu backup và không tạo artifact.

## Ý nghĩa kết quả Wiki

1. **KHÔNG CÓ**: không tìm thấy nhánh Wiki ở cả hai phía, Git trả về kết quả đủ rõ để xác định.
2. **PASS**: Wiki hai phía có và được đối chiếu nhánh, tag, Git objects, LFS.
3. **CHƯA XÁC MINH**: GitHub/GitLab yêu cầu xác thực hoặc chưa thể xác định Wiki có tồn tại. Nếu Git và LFS của repo chính đạt, khi không phát hiện Wiki trên GitLab, job hiển thị **ĐẠT SOURCE CODE**, hoàn tất thành công, nhưng Wiki không được coi là đã PASS.
4. **CHƯA ĐẠT**: nguồn có Wiki mà đích không có/không truy cập được; nguồn thiếu Wiki nhưng đích có Wiki; hoặc Wiki thực sự có sai khác.
5. **Lỗi 429, mạng, 403**: vẫn là lỗi truy cập cần điều tra, không được tự động bỏ qua hoặc đánh dấu PASS.

Nguồn `github.com` được truy cập độc lập, không đưa `GITLAB_TOKEN` và helper GitLab vào tiến trình kiểm tra Wiki GitHub.

## Summary tiếng Việt minh họa

- **Repository:** PASS — Git objects, branches, tags khớp.
- **LFS:** PASS — các object đã tải được hash SHA-256.
- **Wiki:** CHƯA XÁC MINH — GitHub yêu cầu xác thực, có thể Wiki chưa được khởi tạo.
- **Kết luận:** ĐẠT SOURCE CODE — chưa xác minh Wiki.

Trong bảng tổng hợp không dùng artifact, job thành công được ghi **JOB PASS (xem Summary Wiki)**, không mặc nhiên khẳng định Wiki PASS.

## Lưu ý

GitHub chỉ cho clone Wiki sau khi đã tạo trang đầu tiên. Lỗi `could not read Username` không đủ để kết luận Wiki không tồn tại. Vì vậy bản sửa vẫn giữ cảnh báo khi chưa thể biết chắc, thay vì báo PASS Wiki sai. Bản này đã qua test logic cục bộ, chưa kiểm thử live với GitHub/GitLab của bạn.
