# Booklet_AI Desktop — STEP-5.1 Execution Log & Report
**المرحلة:** المرحلة 5 — محرك تقدير التكاليف وعروض الأسعار المجمعة (Cost Estimation & Batch Quoting Engine)  
**الخطوة:** [STEP-5.1] Cost Estimation & Batch Quoting Engine  
**تاريخ التنفيذ:** 2026-09-24  

---

## 1. ملخص ما تم إنجازه (Overview & Summary)

تم بنجاح وبشكل كامل تنفيذ واختبار محرك تقدير التكاليف والتسعير المجمع (Cost Estimation & Batch Quoting Engine) الخاص بخطوة **STEP-5.1**، مع ربطه وتكامله التام مع طبقة التخطيط الهندسي للمستند (Geometry & Margin Engine - STEP-2.1) ومزامنة ترقيم وتصفيف الصفحات الفارغة (Pagination Sync Orchestration):

1. **نماذج بيانات التسعير غير القابلة للتعديل (`Pydantic v2` مع `frozen=True`):**
   - تم إنشاء `PaperStock` لتمثيل مواصفات الورق (الاسم، الوزن بالـ GSM، تكلفة الورقة الواحدة بالدينار العراقي IQD، وسُمك الورقة بالمليمتر `thickness_mm_per_sheet`).
   - تم إنشاء `CostModelConfig` لتمثيل نموذج تكاليف المطبعة (العملة، نسبة التغطية التقديرية للحبر `ink_coverage_estimate_pct`، تكلفة نسبة الحبر للصفحة الواحدة، تكلفة التجليد المقطوعة لكل طريقة تجليد، وشرائح خصم الكميات `volume_discount_tiers`).
   - توفير إعدادات افتراضية مسبقة للأوراق الأكثر شيوعاً في سوق المطابع العراقية (`WOODFREE_70GSM`، `WOODFREE_80GSM`، `GLOSSY_100GSM`، `GLOSSY_150GSM`).

2. **حساب التصفيف الفعلي للورق المطبوع (`Sheet Imposition Calculator`):**
   - طريقة التجليد تحدد عدد الصفحات في الورقة الفيزيائية الواحدة عبر نسبة التصفيف `SHEETS_PER_BOOKLET_RATIO`:
     - السيم الدبابيسي (`saddle_stitch`): 4 صفحات لكل ورقة مطوية (نسبة 0.25).
     - تجليد الغراء الحراري (`perfect_bound`): صفحتان لكل ورقة (نسبة 0.5).
     - التجليد الحلزوني/السلك (`spiral`): صفحة لكل ورقة للمطبوعات الفردية (نسبة 1.0).
   - دالة `imposed_page_count()` تقوم بتقريب عدد الصفحات للأعلى إلى المضاعف الإلزامي لطريقة التجليد (مضاعف 4 للسيم الدبابيسي لتشكيل الملازم المطوية).
   - دالة `sheets_required()` تحسب عدد الأوراق الفيزيائية المطلوبة بناءً على العدد المُصفّف `imposed_page_count` وليس العدد الخام، لضمان عدم التقليل من تكلفة الورق.

3. **حساب سُمك الكعب وهوامش التجليد المشتركة (`Spine Thickness Estimator`):**
   - إغلاق الحلقة الهندسية مع خطوة STEP-2.1: دالة `estimate_spine_mm()` تحسب سُمك الكعب الفعلي بالمليمتر بناءً على عدد الأوراق وسُمك الورقة `PaperStock.thickness_mm_per_sheet`.
   - اشتقاق هامش الكعب الداخلي `inside_spine_mm` في نماذج `MarginConfig` و `PageGeometry` عبر مرجع كائن الإعدادات الموحد `PaperStock` ودوال `calculate_inside_spine_mm()` و `from_paper_stock()` دون أي تكرار للثوابت الرقمية [AC-5.1.3].

4. **عروض الأسعار المفردة والمجمعة مع الإفصاح عن الفرضيات (Disclosed Assumptions Quoting):**
   - دالة `quote_unit()` تحسب التكلفة التقديرية للملزمة الواحدة (تكلفة الورق + تكلفة الحبر المقدرة + تكلفة التجليد)، وتُرجع النتيجة مرفقة بكتلة الفرضيات `assumptions` المفصلة (نسبة تغطية الحبر، عدد الأوراق المستخدمة، نوع وسُمك الورق المستخدم). لا يُعاد رقم سعر مجرد أبداً [AC-5.1.2].
   - دالة `quote_batch()` تحسب عروض الأسعار للطلبيات المجمعة مع تطبيق أعلى شريحة خصم مستحقة عند أو دون الكمية المطلوبة بدقة تامة [AC-5.1.4].

5. **الربط المباشر مع مزامنة الصفحات الفارغة في InDesign (`Pagination Sync Integration`):**
   - استبدال الـ stubs المؤقتة في `src/app/automation/pagination_sync.py` بالاستيراد الحقيقي المباشر لدالة `imposed_page_count` ونوع `BindingMethod` من محرك التسعير [AC-5.1.6].
   - التقاط استثناء تجاوز المحتوى الفعلي للتقدير المبدئي `ContentExceedsEstimateError` بشكل منفصل عن أخطاء التوليد، وإعادة اشتقاق التصفيف الفعلي وإكمال التوليد بنجاح دون اقتطاع للمحتوى [AC-5.1.7].

---

## 2. الملفات التي تم إنشاؤها وتعديلها (Files Created & Modified)

### 1. `src/app/core/pricing_models.py` (إنشاء جديد)
- تعريف نوع `BindingMethod = Literal["saddle_stitch", "perfect_bound", "spiral", "perfect_bind", "wire"]`.
- نموذج `PaperStock(BaseModel)` مع `model_config = ConfigDict(frozen=True)` وتحقق من صحة قيم الـ GSM والتكلفة والسُمك.
- نموذج `CostModelConfig(BaseModel)` مع `model_config = ConfigDict(frozen=True)` ونسب التغطية وشرائح الخصم.
- تعريف الثوابت والمخزونات الافتراضية للمطابع العراقية (`WOODFREE_70GSM`, `WOODFREE_80GSM`, `GLOSSY_100GSM`, `GLOSSY_150GSM`, `DEFAULT_PAPER_STOCK`).

### 2. `src/app/core/pricing_engine.py` (إنشاء جديد)
- جداول التصفيف `SHEETS_PER_BOOKLET_RATIO` ومضاعفات الملازم `IMPOSITION_MULTIPLE`.
- دالة التصفيف `imposed_page_count(page_count, binding)` مع معالجة الأسماء البديلة والتحقق الصارم من المدخلات.
- فئة المحرك `PricingEngine`:
  - `sheets_required(page_count, binding)`: حساب عدد الأوراق الفيزيائية الفعلية بناءً على العدد المُصفّف.
  - `estimate_spine_mm(page_count)`: تقدير سُمك كعب الملزمة بالمليمتر.
  - `calculate_inside_spine_margin(page_count, base_inside_margin_mm, min_margin_mm)`: حساب هامش الكعب الداخلي بناءً على نفس سُمك الورقة.
  - `create_margin_config(...)`: توليد كائن `MarginConfig` لهندسة صفحات InDesign مباشرة.
  - `quote_unit(page_count, binding)`: حساب سعر الملزمة الواحدة مع كتلة الفرضيات المعلنة.
  - `quote_batch(page_count, binding, quantity)`: حساب عروض الأسعار بالكميات مع شرائح الخصم التلقائية.

### 3. `src/app/automation/geometry.py` (تعديل وتحديث)
- استيراد `PaperStock`.
- إضافة دالة `calculate_inside_spine_mm(page_count, paper, base_margin_mm, min_margin_mm)`.
- إضافة `MarginConfig.from_paper_stock(...)` لاشتقاق هوامش الصفحات من سُمك الورق المشترك.
- إضافة `PageGeometry.from_paper_stock(...)` لإنشاء هندسة الصفحة المتكاملة من سُمك الورق.

### 4. `src/app/automation/pagination_sync.py` (تعديل وتحديث)
- حذف الـ stubs المؤقتة واستيراد `BindingMethod` و `imposed_page_count` فعلياً من `app.core.pricing_models` و `app.core.pricing_engine`.
- ربط `sync_filler_pages()` بمحرك التسعير الفعلي.

### 5. `src/app/core/__init__.py` (تعديل وتحديث)
- تصدير كافة نماذج التسعير ودوال ومحرك التسعير في واجهة وحدة `app.core`.

### 6. `booklet_ai.spec` و `src/booklet_ai.spec` (تحديث مواصفات البناء والتجميع)
- إضافة `"app.core.pricing_models"` و `"app.core.pricing_engine"` إلى قائمة `hiddenimports`.

### 7. `src/tests/test_step_5_1.py` (إنشاء جديد)
- 24 اختبار وحدة متقدم وتكاملي يغطي كل معيار قبول بالتفصيل.

---

## 3. نتائج الاختبارات والتحقق (Test Results & Verification)

1. **اختبارات وحدة STEP-5.1 الجديدة:**
   ```
   pytest tests/test_step_5_1.py -v
   24 passed in 0.27s
   ```
2. **كامل جناح اختبارات المشروع (من STEP-1.1 وحتى STEP-5.1):**
   ```
   pytest
   359 passed in 6.09s
   ```
   جميع الاختبارات السابقة البالغة 335 اختباراً ظلت تعمل بنجاح 100% دون أي انكسار أو تراجع في أي ميزة سابقة.

---

## 4. مراجعة معايير القبول (Acceptance Criteria Verification)

| معيار القبول | نص المعيار في الخطة | هل تم تحقيقه؟ | التفاصيل والملاحظات الصريحة |
|---|---|:---:|---|
| **[AC-5.1.1]** | `sheets_required()` produces different results for identical `page_count` across the three `binding_method` values, reflecting each method's actual imposition ratio. | **نعم** | تم التحقق في `test_sheets_required_distinct_across_methods_ac_5_1_1`. لعدد صفحات 20: سيم الدبابيس يعطي 5 أوراق (نسبة 0.25)، التجليد بالغراء يعطي 10 أوراق (نسبة 0.5)، والتجليد الحلزوني يعطي 20 ورقة (نسبة 1.0). القيم الثلاث مختلفة تماماً وتعكس تصفيف كل نوع تجليد. |
| **[AC-5.1.2]** | Every quote output (`quote_unit`/`quote_batch`) includes an `assumptions` block — no bare price number is returned without its disclosed estimation basis. | **نعم** | تم التحقق في `test_quote_unit_structure_and_assumptions_ac_5_1_2` و `test_batch_quote_includes_assumptions_ac_5_1_2`. المخرجات في الدالتين تتضمن دائماً قاموس `assumptions` يحتوي على `ink_coverage_pct`، `sheets_used`، و `paper_stock` ولا يُعاد سعر بمفرده إطلاقاً. |
| **[AC-5.1.3]** | `estimate_spine_mm()` and STEP-2.1's `inside_spine_mm` margin calculation both derive from the same `PaperStock.thickness_mm_per_sheet` source value — verified by a shared config reference, not duplicated constants. | **نعم** | تم التحقق في `test_shared_config_reference_ac_5_1_3`. كلاهما يستمد قيمة سُمك الورقة حصراً من مرجع كائن `PaperStock.thickness_mm_per_sheet` عبر `calculate_inside_spine_mm()` و `MarginConfig.from_paper_stock()`. تغيير سُمك الورقة في الكائن يغير سُمك الكعب وهامش الكعب معاً دون أي ثوابت مكررة. |
| **[AC-5.1.4]** | `quote_batch()` correctly applies the highest discount tier at or below the requested quantity (e.g. quantity=200 applies the 10% tier, not the 5% or 15% tier). | **نعم** | تم التحقق في `test_volume_discount_tier_application_ac_5_1_4`. عند طلب كمية 200 نسخة، تم تطبيق شريحة 10% بدقة متناهية، وليس شريحة 5% (للكمية 50-199) ولا شريحة 15% (للكمية 500+). |
| **[AC-5.1.5]** | For `binding_method = "saddle_stitch"`, `imposed_page_count(17, "saddle_stitch")` returns `20` (rounded up to the next multiple of 4), and `sheets_required(17, "saddle_stitch")` computes sheets from that rounded value, not the raw input. | **نعم** | تم التحقق في `test_imposed_page_count_saddle_stitch_ac_5_1_5` و `test_sheets_required_uses_imposed_page_count_ac_5_1_5`. تم إثبات أن 17 صفحة تقرب إلى 20 صفحة، ودالة `sheets_required` تعتمد الرقم 20 لإنتاج 5 أوراق بدلاً من الاعتماد على العدد الخام. |
| **[AC-5.1.6]** | `imposed_page_count()`'s result for a saddle-stitch job is the same value used to determine the final blank-filler-page count added to the actual InDesign document — verified by the actual `sync_filler_pages()` orchestration (STEP-2.1) calling `imposed_page_count()` directly and passing its result to `addBlankFillerPages()`, not by two independently-maintained rounding calculations. | **نعم** | تم التحقق في `test_sync_filler_pages_calls_pricing_imposed_count_ac_5_1_6`. تم حذف الـ stub القديمة من `pagination_sync.py` واستيراد دالة `imposed_page_count()` مباشرة من `pricing_engine.py`، وتم التأكد من تمرير القيمة المحسوبة مباشرة إلى استدعاء ExtendScript `addBlankFillerPages()`. |
| **[AC-5.1.7]** | When actual generated content exceeds the pre-layout page estimate (e.g. 22 actual pages vs. a 20-page estimate), `sync_filler_pages()` catches `ContentExceedsEstimateError` distinctly from a genuine filler-insertion failure, recomputes `imposed_page_count()` against the actual count, and successfully completes generation with a corrected target — never silently truncates real content pages and never reports this as the same error class as an actual filler-page bug. | **نعم** | تم التحقق في `test_sync_filler_pages_content_overrun_handling_ac_5_1_7` و `test_sync_filler_pages_genuine_error_not_caught_as_overrun`. عند حدوث تجاوز المحتوى لـ 22 صفحة مقابل تقدير 20 صفحة، يلتقط الكود `ContentExceedsEstimateError` بشكل منفصل، ويقرأ عدد الصفحات الفعلي عبر `getPageCount`، ويعيد حساب التصفيف إلى 24 صفحة، ويكمل التوليد بنجاح دون اقتطاع، بينما تمر أخطاء الـ InDesign الأصلية دون اعتراض خاطئ. |
