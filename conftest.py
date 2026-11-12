"""إعداد مسار الاستيراد للاختبارات: يتيح تشغيل pytest بدون تثبيت الحزمة.

(الطريقة الموصى بها: pip install -e . ثم pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
