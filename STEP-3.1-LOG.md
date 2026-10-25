# Booklet_AI Desktop — STEP-3.1 Execution Log & Report
**المرحلة:** المرحلة 3 — بنك الأسئلة الوزارية (Question Bank) والبحث الدلالي
**الخطوة:** [STEP-3.1] Embedded Vector Store & Embedding Pipeline
**تاريخ التنفيذ:** 2026-09-24

---

## 1. ملخص ما تم إنجازه (Overview & Summary)

تم تنفيذ جميع متطلبات الخطوة **STEP-3.1** بالكامل لبناء وتكامل مخزن المتجهات المدمج (Embedded Vector Store) وخط أنابيب التضمين (Embedding Pipeline) للأسئلة الوزارية (`ministry_question`) مع الحفاظ التام على قواعد المشروع الصارمة:
- **Zero-Mutation:** لا يتم تعديل محتوى النص الأصلي `raw_content` أو الهاش القانوني `content_hash` نهائياً.
- **Append-Only:** البيانات في جدول الأسئلة الوزارية تصاعدية وغير قابلة للتعديل المباشر في مكانها؛ وأي تصحيح يتم عبر صف جديد يربط بالسابق عبر `superseded_by`.
- **Zero-Service Architecture:** تم اعتماد `SQLite` ومكتبة الامتداد المدمجة `sqlite-vec`، حيث يعمل كل شيء داخل نفس المعالج (In-Process) بدون أي خوادم خارجية (كـ PostgreSQL أو pgvector)، وبدون فتح أي منافذ شبكة أو خدمات ويندوز.
- **Audited Graceful Degradation:** في حالة انقطاع الإنترنت أو فشل استدعاء التضمين عبر OpenRouter، لا تتوقف دورة توليد الملزمة إطلاقاً، بل يتم حفظ السؤال فوراً مع وسم `pending_embedding = 1` ليتم استكماله لاحقاً (Backfill).
- **Fail-Fast Isolation:** في حالة فشل الكتابة في قاعدة البيانات (خطأ فني في القرص أو الاتصال)، يتم عمل `rollback` فوري وإطلاق استثناء `QuestionIndexError` ليتم إظهاره كتحذير غير مانع للاستمرار في التوليد.

---

## 2. الملفات التي تم إنشاؤها وتعديلها (Files Created & Modified)

### 1. `src/app/core/exceptions.py` (تعديل)
- تمت إضافة فئة الاستثناء `QuestionIndexError(BookletError)` لتمثيل أي فشل غير قابل للاسترداد أثناء كتابة أو قراءة بيانات بنك الأسئلة في قاعدة البيانات.

### 2. ملفات الـ Migration:
- `src/migrations/001_ministry_questions.sql`
- `migrations/001_ministry_questions.sql`
- يحتوي الملف على:
  - إنشاء جدول `ministry_questions` مع قيد فريد `content_hash UNIQUE`، وحقول التتبع (`block_id`, `source_doc_id`, `source_page`, `subject`, `grade_level`, `pending_embedding`, `superseded_by`, `created_at`).
  - إنشاء الجدول الافتراضي الموجه للمتجهات عبر `sqlite-vec`:
    `CREATE VIRTUAL TABLE IF NOT EXISTS ministry_questions_vec USING vec0(embedding FLOAT[1536]);`

### 3. `src/app/utils/db.py` (إنشاء جديد)
- إدارة الاتصال بقاعدة البيانات SQLite المدمجة:
  - تحديد المسار التلقائي للملف في بيانات التطبيق `%APPDATA%/Booklet_AI/question_bank.db`.
  - تحميل امتداد `sqlite-vec` تلقائياً عبر `sqlite_vec.load(conn)` مع تفعيل `enable_load_extension`.
  - تفعيل القيود المرجعية `PRAGMA foreign_keys = ON;` ونمط `WAL` لتسريع القراءة والكتابة.
  - مشغل ترحيلات خفيف الوزن (Lightweight Migration Runner) يعتمد على جدول `schema_version` لضمان تطبيق ملفات الترحيل `.sql` بالترتيب وبشكل idempotent.
  - دالة `init_db(db_path)` التي تفتح الاتصال وتطبق الترحيلات دفعة واحدة.

### 4. `src/app/ai/embedding_client.py` (إنشاء جديد)
- عميل OpenRouter لتوليد التضمينات النصية المتوافقة مع مواصفات OpenAI (`openai/text-embedding-3-small` بأبعاد 1536):
  - توليد فردي `embed(text)` ودفعي `embed_batch(texts)`.
  - التحقق من المفتاح والمدخلات.
  - معالجة أخطاء الشبكة وتحويلها إلى `EmbeddingError`.

### 5. `src/app/ai/question_indexer.py` (إنشاء جديد)
- نموذج البيانات غير القابل للتعديل: `QuestionRecord(BaseModel)` مع `frozen=True`.
- صنف `QuestionIndexer`:
  - `index_block(block, source_doc_id, source_page, subject, grade_level)`:
    - التحقق أولاً من أن نوع الكتلة هو `ministry_question`.
    - الفحص السريع لتكرار الهاش `content_hash` في قاعدة البيانات دون إجراء أي اتصال بالذكاء الاصطناعي (توفيراً للتكلفة).
    - محاولة توليد التضمين، مع الانحدار الهادئ إلى `pending_embedding = 1` عند أي فشل للشبكة.
    - كتابة السؤال والمتجه في معاملة واحدة وتأكيدها `commit`، أو `rollback` وإطلاق `QuestionIndexError` عند فشل قاعدة البيانات.
  - `index_document(doc, subject, grade_level, on_error)`: فهرسة كتل المستند بالكامل مع التقاط الأخطاء وتمريرها لكولباك التحذير دون إيقاف التوليد.
  - `find_similar(query, k, threshold)`: البحث الدلالي السريع KNN عبر جدول `ministry_questions_vec` وإرجاع النتائج مرتبة تصاعدياً حسب المسافة `distance`.
  - `find_near_duplicates(block, threshold, k)`: كشف الأسئلة المكررة تقريبياً عبر السنوات للمراجعة البشرية.
  - `backfill_pending(batch_size)`: استكمال المتجهات للأسئلة المعلقة عند عودة الاتصال.
  - `mark_superseded(old_id, new_id)`: ربط السجلات القديمة بالجديدة تصاعدياً.
  - دوال الاستعلام: `get_by_id`, `get_by_hash`, `count_questions`, `count_pending_embeddings`.

### 6. `src/tests/test_step_3_1.py` (إنشاء جديد)
- تم بناء جناح اختبارات متكامل يغطي 22 حالة اختبار متقدمة تغطي كل المعايير من AC-3.1.1 إلى AC-3.1.5 بالتفصيل.

---

## 3. نتائج الاختبارات (Testing & Benchmark Results)

- تم تشغيل الاختبارات الجديدة لـ STEP-3.1 بنجاح كامل:
  `src/tests/test_step_3_1.py: 22 passed in 3.10s`
- تم تشغيل كامل جناح اختبارات المشروع (STEP-1.1 وحتى STEP-3.1) وتأكيد عدم وجود أي انكسار أو تراجع:
  `301 passed in 6.09s`
- اختبار الأداء (AC-3.1.4):
  - استعلام KNN على 50,000 متجهة بأبعاد 1536 باستخدام `sqlite-vec` استغرق **183.85 ملي ثانية** (أقل من الحد الأقصى 200ms).

---

## 4. مراجعة معايير القبول (Acceptance Criteria Verification)

| معيار القبول | الوصف | محقق؟ | الملاحظات والتفاصيل |
|---|---|---|---|
| **[AC-3.1.1]** | إعادة معالجة ملزمة مفهرسة مسبقاً لا تُدخل أي صفوف مكررة (منع التكرار عبر قيد `content_hash UNIQUE` قبل أي استدعاء لتضمين المتجهات). | **نعم** | يتم فحص الهاش القانوني في قاعدة البيانات فوراً، وفي حال وجوده يتم التجاوز فوراً بدون استهلاك أي توكن أو استدعاء للـ API، وتم إثبات ذلك باختبارات الوحدة. |
| **[AC-3.1.2]** | فشل استدعاء واجهة التضمين (API) لا يطلق أي استثناء خارج `index_block()` ولا يعطل توليد المستند — بل يتم حفظ السؤال مع `pending_embedding = 1`. | **نعم** | يتم اعتراض أي استثناء صادر من عميل التضمين داخلياً وتعيين `pending = True` وحفظ السؤال في جدول SQLite بدون متجهة، مع إتاحة دالة `backfill_pending` لمعالجتها لاحقاً. |
| **[AC-3.1.3]** | فشل الكتابة في قاعدة البيانات (بشكل مستقل عن فشل التضمين) يُطلق `QuestionIndexError` ويعمل `rollback` للمعاملة، ويظهر للمستخدم كشريط تحذير غير مانع (يستمر التوليد مع تجاوز الفهرسة لتلك الكتلة فقط). | **نعم** | محمي عبر `try...except` يعمل `db.rollback()` ويرفع `QuestionIndexError`، وفي `index_document` يتم التقاطه واستدعاء كولباك `on_error` لمواصلة باقي الكتل. |
| **[AC-3.1.4]** | بحث التشابه KNN على `ministry_questions_vec` يرجع النتائج مرتبة بالمسافة في أقل من 200ms لبنك يحوي ≤50,000 صف على عتاد سطح المكتب النموذجي. | **نعم** | تم قياس الاستعلام الفعلي على 50,000 متجه بطول 1536 بُعد وبلغ زمن الاسترجاع **183.85ms** (داخل الحدود المحددة). |
| **[AC-3.1.5]** | لا يتطلب التطبيق أي خدمات خارجية، منافذ مفتوحة، أو خوادم قواعد بيانات مثبتة لتعمل هذه الميزة — تم التحقق بتشغيل مسار الفهرسة والبحث بالكامل محلياً داخل العملية. | **نعم** | قاعدة بيانات SQLite محلية مدمجة كملف واحد مع امتداد `sqlite-vec` المدمج كـ DLL/C-extension داخل بايثون، بدون أي خدمات خلفية أو اتصالات شبكية عدا طلب التضمين الخارجي. |
