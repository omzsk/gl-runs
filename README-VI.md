# Restore Test — Summary dễ đọc theo từng repository

## Giao diện Summary giống ví dụ GitHub Actions

Mỗi job có tên **Restore <tên-repository>** và Summary dạng dòng ngắn:

- **Repository:** PASS / CHƯA ĐỒNG BỘ / LỆCH REFS / LỖI DỮ LIỆU ...
- **LFS:** PASS (số object, byte, SHA-256) hoặc CHƯA XÁC NHẬN
- **Wiki:** PASS / KHÔNG CÓ / BỎ QUA / có lỗi
- **Kết luận:** ĐẠT hoặc chưa đạt, không gán nhầm corrupt khi chỉ có commit mới.

Khi có khác biệt, ngay dưới các dòng trên sẽ xuất hiện phần **Chi tiết lỗi**: hạng mục/bước, tên ref, loại sai khác, SHA GitHub/GitLab, số commit chậm, thời gian commit (trong bảng mở rộng), thông báo lỗi Git/LFS, OID bị thiếu, và gợi ý xử lý. Bảng dài đặt trong mục mở rộng để Summary gọn. JSON artifact giữ toàn bộ chi tiết trong 30 ngày.

Job **Tổng hợp Restore Test — tiếng Việt** hiển thị số repo ĐẠT, CHƯA ĐỒNG BỘ, LỆCH REFS, THIẾU LFS, LỖI DỮ LIỆU, CHƯA XÁC MINH; đồng thời có mô tả lỗi theo repository và danh sách đầy đủ có thể mở rộng.

## Cập nhật

Chỉ cần thay hai file sau trong repository GitHub Actions đang backup:

1. `.github/workflows/restore-test.yml`
2. `scripts/verify_restore_full.py`

Giữ nguyên `.github/workflows/sync.yml`, `repos`, `scripts/parse_repos.py`, và `GITLAB_TOKEN`, `GITLAB_NAMESPACE`, `GITLAB_HOST`. Không cần thay workflow backup. Workflow test chỉ đọc GitHub/GitLab; không push hoặc xóa dữ liệu.

Chạy tại **GitHub → Actions → GitLab Full Restore Test → Run workflow**. Chọn `single`, `weekly` hoặc `all`.

## Giới hạn

- `CHƯA ĐỒNG BỘ` nghĩa là nhánh GitLab là tổ tiên của GitHub, **không phải data corrupt**; kết quả chung vẫn không ĐẠT vì chưa đồng bộ.
- Với ref khác nhau, script vẫn chạy Git fsck nhưng **chưa tải/kiểm tra toàn bộ LFS**, Summary hiển thị CHƯA XÁC NHẬN để tránh báo PASS nhầm.
- `KHÔNG CÓ` Wiki nghĩa là không thấy nhánh Wiki ở hai phía; nếu Wiki thực tế có dữ liệu mà bị hạn chế truy cập, cần kiểm tra quyền riêng.
- Khi ref khớp, workflow kiểm tra toàn bộ object LFS có thể truy xuất từ các refs bằng việc tải và hash SHA-256 (có thể tốn thời gian, băng thông và phát sinh rate limit).
- Phạm vi gồm Git refs, Git objects, Git LFS và Git Wiki, không bao gồm Issues, PR, Release assets hay metadata.
- Commit time không phải thời điểm push/backup. Chưa có thử nghiệm trực tiếp với GitLab của bạn; thử nghiệm local không xác minh mạng và Git LFS thật.

## Ý nghĩa các trạng thái

| Trạng thái | Ý nghĩa | Hướng xử lý |
|---|---|---|
| **ĐẠT** | Refs khớp và Git fsck, LFS hash đã đạt; Wiki nếu có cũng được kiểm tra | Không cần làm gì |
| **CHƯA ĐỒNG BỘ** | SHA GitLab là tổ tiên của SHA GitHub | Chạy backup tiếp theo, kiểm tra lại |
| **LỆCH REFS** | Branch/tag bị thiếu, dư, khác SHA hoặc khác nhánh mặc định | Mở mục Chi tiết lỗi, xem SHA từng phía |
| **THIẾU LFS** | Không có object được tham chiếu hoặc object không tải được | Xem LFS OID trong Summary và log backup |
| **LỖI DỮ LIỆU** | Git fsck hoặc SHA-256 LFS phát hiện lỗi | Ưu tiên xử lý, thử restore lại |
| **CHƯA XÁC MINH** | Rate limit, token, nguồn thay đổi, lỗi mạng hoặc chưa hoàn thành | Chạy lại khi GitHub/GitLab ổn định |

**Lưu ý:** Job tổng thể vẫn Failed nếu có repository chưa ĐẠT, kể cả khi chỉ chậm đồng bộ. Các tag `__mirror_backup__` ở GitLab được giữ và không bị coi là ref dư. Thời gian commit không phải thời gian backup. Với ref khác nhau, bước LFS được ghi CHƯA XÁC NHẬN thay vì PASS.

## Test cục bộ

Hai test đi kèm: `tests/test_full_local.py` kiểm tra Git refs/fsck và mô phỏng mạng LFS; `tests/test_report_readable.py` kiểm tra văn bản Summary khi PASS, lệch refs, thiếu LFS, snapshot thay đổi và tổng hợp. Không có xác minh live GitLab trong test cục bộ.
