# Booklet_AI Desktop — STEP-6.2 Execution Log & Report
**المرحلة:** المرحلة 6 — الواجهة وتجربة المستخدم والإعدادات (UI & Experience)  
**الخطوة:** [STEP-6.2] Field Error Translation, Reporting & Auto-Recovery  
**تاريخ التنفيذ:** 2026-10-19  

---

## 1. ملخص ما تم إنجازه (Overview & Summary)

تم بنجاح وبشكل كامل تنفيذ واختبار نظام ترجمة الأخطاء الميدانية، إعداد تقرير المشكلة، والتعافي التلقائي (Field Error Translation, Reporting & Auto-Recovery) الخاص بخطوة **STEP-6.2**، مع تصحيح كافة العيوب المعمارية الأربعة المحددة في مرحلة المراجعة (Post-Review Corrections):

1. **طبقة ترجمة الأخطاء عند حدود العرض (`UI Presentation Boundary`) عبر شجرة الوراثة (`MRO-based`):**
   - تم إنشاء فئة `UserFacingError(Exception)` التي تحتفظ بالرسالة العربية المترجمة للمشغل بالإضافة إلى كائن الاستثناء الأصلي غير المترجم `original` بكامل تفاصيله التقنية دون أي فقدان للمعلومات التشخيصية.
   - تم بناء جدول الترجمات الثابت `ERROR_TRANSLATIONS` الذي يربط كافة فئات الاستثناءات المعروفة في المراحل 1–5 (`TextIntegrityError`, `MathExtractionError`, `COMOperationTimeout`, `InDesignUnavailableError`, `InvalidBodyFrameGeometryError`, `ContentExceedsEstimateError`, `MarginConfigError`, `StyleMappingError`, `RTLTableUnsupportedError`, `QuestionIndexError`, `BookletError`, وغيرها) برسائل عربية واضحة ومحددة موجهة لمشغل المطبعة غير التقني.
   - دالة `translate_error(exc)` تتنقل عبر شجرة وراثة الصنف `type(exc).__mro__` (من الصنف الأخص إلى الأعم) بدلاً من المطابقة الحرفية للنوع، مما يضمن أن أي فئة مشتقة مستقبلية لأي خطأ معروف ترث ترجمة الصنف الأب تلقائياً، وفي حال عدم وجود أي تطابق في الـ MRO يتم الرجوع إلى رسالة الخطأ العامة `FALLBACK_MESSAGE`.

2. **التنقيح والتعمية الإلزامية المركزية للبيانات الحساسة (`Centralized Report Redaction`):**
   - تم بناء `app/ui/report_redaction.py` مع دالة `redact(text)` المركزية التي تطبق تعابير نمطية صارمة وشاملة لحجب المفاتيح والرموز السرية:
     - رموز الـ Bearer: `Bearer [REDACTED]`
     - مفاتيح API الخاصة بـ OpenRouter و OpenAI (مثل `sk-...`, `sk-or-v1-...`, `or-...`): `[REDACTED_API_KEY]`
     - ترويسات التفويض `Authorization`: `"Authorization": "[REDACTED]"`
     - الرموز العشوائية الطويلة (tokens): `[REDACTED_TOKEN]`
   - هذا الضمان معماري وهيكلي، حيث يمر كل حقل نصي في تقرير المشكلة تلقائياً عبر `redact()` داخل `build_problem_report()` ولا يُعتمد على مجرد افتراض أن بقية الكود لا يسجل أسراراً.

3. **تقرير المشكلة المستقل والقابل للمشاركة (`ProblemReport Artifact`):**
   - تم إنشاء نموذج `ProblemReport` كعقد Pydantic v2 مجمّد بالكامل (`frozen=True`) يحتوي على: `generated_at`, `app_version`, `correlation_id`, `original_exception_type`, `original_exception_message`, و `recent_log_lines`.
   - دالة `build_problem_report(exc, app_version, correlation_id, recent_log_lines)` تقوم بفك تغليف `UserFacingError` للوصول إلى الاستثناء التقني الأصلي واستخراج اسمه ورسالته المنقحة، مع تنقيح أسطر السجل الحديثة.
   - دالة `export_problem_report_json(report)` لتصدير التقرير بتنسيق JSON نظيف وسليم وترميز UTF-8 آمن.

4. **آلية التعافي التلقائي المحكومة بدقة بقائمة السماح (`Allow-list Auto-Recovery`):**
   - اعتماد قائمة سماح صريحة (`AUTO_RETRYABLE_EXCEPTIONS`) تقتصر فقط على `COMOperationTimeout` (الأخطاء البيئية/العابرة المحتملة في InDesign)، وقائمة منع صريحة نهائية (`NEVER_AUTO_RETRY`) تضم `TextIntegrityError` و `MathExtractionError` التي يستحيل نجاح إعادة المحاولة فيها بالمدخلات نفسها.
   - تقييد عدد المحاولات بمرة واحدة صامتة فقط (`MAX_AUTO_RETRIES = 1`).
   - إنشاء نموذج `SupervisorRetryState` المجمّد لتسجيل وقائع التنظيف التي يجريها المشرف (`child_process_exited`, `owned_indesign_pid`, `owned_indesign_confirmed_terminated`, `attached_to_pre_existing_instance`, `partial_output_path`).
   - دالة `is_auto_retryable(exc, supervisor_state)` تشترط لـ `COMOperationTimeout` تحقق `confirms_clean_retryable_state()`؛ وإذا ارتبطت العملية بنسخة InDesign سابقة للمستخدم (`attached_to_pre_existing_instance=True`) أو وُجد ملف مخرجات جزئي (`partial_output_path is not None`) أو لم يتم إثبات نظافة الحالة، تُرفض إعادة المحاولة فوراً حمايةً لبيئة المستخدم.

5. **تحديث `COMSupervisor` لربط حالة التنظيف بالاستثناء (`SupervisorRetryState Attachment`):**
   - تحديث دالة `_terminate_and_cleanup()` لتعيد قاموساً دقيقاً بوقائع عملية الإنهاء والتنظيف.
   - تحديث موقع إطلاق `COMOperationTimeout` (عند انتهاء المهلة أو كود الخروج غير الصفري) ليقوم بإنشاء كائن `SupervisorRetryState` وربطه بالاستثناء في الخاصية `supervisor_state`، مما يحل الفجوة التي كانت تجعل المشرف يتخلص من هذه البيانات.

6. **أوركسترا إعادة المحاولة المنفصلة وحفظ تعديلات المستخدم (`Job Retry Orchestration`):**
   - تم إنشاء `app/automation/job_retry.py` مع دالة `run_job_with_retry()`.
   - يتم تشغيل الاستخراج والتصنيف (`_extract_and_classify`) **مرة واحدة فقط** لكل تقديم مهمة، خارج حلقة إعادة المحاولة، بحيث لا تُعاد معالجة الملف واستنزاف الوقت والذكاء الاصطناعي على خطأ downstream في InDesign.
   - إعادة المحاولة تشمل **فقط** مرحلة التوليد عبر InDesign COM، وتُعيد استخدام نفس كائن المستند المخزن في `DocumentStore` بكل تعديلات المستخدم السابقة `with_user_edit()` كما كانت، دون فقدان أي عمل قام به المشغل في شاشة المعاينة.

---

## 2. الملفات التي تم إنشاؤها وتعديلها (Files Created & Modified)

### 1. `src/app/ui/error_translation.py` (إنشاء جديد)
- فئة `UserFacingError(Exception)`.
- قاموس `ERROR_TRANSLATIONS` لكافة استثناءات المراحل 1–5.
- نص الفولباك العام `FALLBACK_MESSAGE`.
- دالة `translate_error(exc)` المعتمدة على الـ MRO.

### 2. `src/app/ui/report_redaction.py` (إنشاء جديد)
- قائمة تعابير التنقيح `_REDACTION_PATTERNS`.
- دالة `redact(text)` لتنقيح مفاتيح API، ورموز Bearer، وترويسات Authorization.

### 3. `src/app/ui/problem_report.py` (إنشاء جديد)
- نموذج `ProblemReport` المجمّد بـ Pydantic v2.
- دالة `build_problem_report()` المنقحة لجميع الحقول.
- دالة `export_problem_report_json()`.

### 4. `src/app/utils/auto_recovery.py` (إنشاء جديد)
- نموذج `SupervisorRetryState`.
- قوائم `AUTO_RETRYABLE_EXCEPTIONS` و `NEVER_AUTO_RETRY` و `MAX_AUTO_RETRIES`.
- دالة `is_auto_retryable()`.

### 5. `src/app/automation/com_supervisor.py` (تعديل وتحديث)
- تحديث `_terminate_and_cleanup()` لتعيد قاموس تفاصيل التنظيف.
- تحديث مواقع إطلاق `COMOperationTimeout` لربط `SupervisorRetryState` بالاستثناء في حقل `supervisor_state`.

### 6. `src/app/automation/job_retry.py` (إنشاء جديد)
- دالة `run_job_with_retry()` لتنسيق وإدارة إعادة محاولة مرحلة التوليد فقط مع الحفاظ على تعديلات المشغل.

### 7. تحديث التصديرات وملفات التجميع:
- `src/app/ui/__init__.py`: تصدير دوال ونماذج الترجمة والتقرير والتنقيح.
- `src/app/utils/__init__.py`: تصدير نماذج وقوائم التعافي التلقائي.
- `src/app/automation/__init__.py`: تصدير `run_job_with_retry` و `COMSupervisor`.
- `booklet_ai.spec` و `src/booklet_ai.spec`: إضافة الموديولات الجديدة إلى `hiddenimports`.

### 8. `src/tests/test_step_6_2.py` (إنشاء جديد)
- 44 اختباراً شاملاً يغطي بدقة معايير القبول من AC-6.2.1 وحتى AC-6.2.7b.

---

## 3. نتائج الاختبارات (Test Verification)

- **اختبارات STEP-6.2 المستهدفة:** 44 اختباراً تم اجتيازها بنجاح (100%).
- **إجمالي اختبارات المشروع الكلية:** 461 اختباراً تم اجتيازها بنجاح (100%) بدون أي أخطاء أو تراجعات في المكونات السابقة.
