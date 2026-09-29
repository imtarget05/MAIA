"""Seed hai user demo thuộc HAI tenant khác nhau, để test cross-tenant thật.

Vì sao cần script này
---------------------
Control quan trọng nhất của MAIA về tenant là: `tenant_id` LUÔN lấy từ user
đã xác thực, không bao giờ lấy từ request body (xem `POST /query`). Nhưng
muốn chứng minh control đó trên cloud thì phải có hai user thật ở hai tenant
khác nhau, đăng nhập được, rồi thử lấy dữ liệu chéo.

Không có cách nào làm điều đó chỉ với token giả: token giả bị chặn ở tầng auth,
nên ta sẽ chỉ chứng minh được "bị chặn vì token sai", chứ không phải "bị chặn
vì sang tenant". Hai claim đó khác nhau, và claim thứ hai mới là cái đáng giá.

Về an toàn
----------
KHÔNG có credential nào nằm trong mã nguồn. Script đọc từ biến môi trường
(DEMO_USER_A_EMAIL, DEMO_USER_A_PASSWORD, ...) và chỉ chạy khi
DEMO_SEED_ENABLED=true. Secret được đưa vào Azure bằng secret của Container
Apps, không commit vào repo.

Cũng cố tình KHÔNG seed dữ liệu nghiệp vụ. Demo này chạy /ready ở chế độ
degraded vì không có Qdrant, nên seed mấy bản ghi "tài liệu" chỉ để làm ra
vẻ có dữ liệu. Hai user là tối thiểu cần thiết để kiểm tra chặn tenant; nhiều
hơn thế là dựng một hệ thống mà claim của repo không hỗ trợ.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy.orm import Session  # noqa: E402

from maia.auth import get_password_hash  # noqa: E402
from maia.models import Base, User, UserRole  # noqa: E402


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"refusing to seed: {name} is not set")
    return value


def main() -> int:
    if os.environ.get("DEMO_SEED_ENABLED", "").lower() != "true":
        print("DEMO_SEED_ENABLED is not true; nothing seeded")
        return 0

    from maia.api import engine  # noqa: PLC0415  (dùng chung engine với app)

    specs = [
        ("A", required("DEMO_USER_A_EMAIL"), required("DEMO_USER_A_PASSWORD"),
         required("DEMO_USER_A_TENANT")),
        ("B", required("DEMO_USER_B_EMAIL"), required("DEMO_USER_B_PASSWORD"),
         required("DEMO_USER_B_TENANT")),
    ]

    # Kiểm tra TRƯỚC khi ghi bất cứ thứ gì. Hai user cùng tenant thì bài test
    # cross-tenant vẫn "chạy" và vẫn xanh, chỉ là nó không chứng minh được điều
    # gì — đó là loại test nguy hiểm hơn cả là không test.
    if specs[0][3] == specs[1][3]:
        raise SystemExit("refusing to seed: both users share a tenant, "
                         "which would make a cross-tenant test meaningless")

    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        for label, email, password, tenant in specs:
            if db.query(User).filter(User.email == email).first():
                print(f"user {label}: already present, left untouched")
                continue
            db.add(
                User(
                    email=email,
                    password_hash=get_password_hash(password),
                    role=UserRole.USER,
                    tenant_id=tenant,
                    is_active=True,
                    email_verified=True,
                )
            )
            print(f"user {label}: created in tenant {tenant}")
        db.commit()

    print("seed complete: two users, two distinct tenants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
