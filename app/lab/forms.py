from django import forms

from .models import Field, Job


class JobForm(forms.ModelForm):
    class Meta:
        model = Job
        fields = ["title", "customer", "sample_code", "test_series"]


class UploadForm(forms.Form):
    file = forms.FileField(label="Source PDF")
    form_type = forms.ChoiceField(required=False, label="Form to extract")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .extraction import schemas

        self.fields["form_type"].choices = [("", "Store document only")] + [
            (s["form_type"], s["form_type"].replace("_", " ").title()) for s in schemas()
        ]


class FieldForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, initial=0)

    class Meta:
        model = Field
        fields = [
            "key",
            "label",
            "value",
            "unit",
            "origin",
            "document",
            "page",
            "status",
            "version",
        ]
        widgets = {"value": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, job, **kwargs):
        super().__init__(*args, **kwargs)
        self.job = job
        self.fields["document"].queryset = job.documents.all()
        if self.instance.pk:
            # Corrections may change the reading and its review state, but
            # never rewrite the identity or source of an existing record.
            for name in ("key", "label", "origin", "document", "page"):
                self.fields[name].disabled = True

    def clean(self):
        data = super().clean()
        if self.job.fields.filter(key=data.get("key")).exclude(pk=self.instance.pk).exists():
            self.add_error("key", "This field already exists in the job.")
        doc, page = data.get("document"), data.get("page")
        if data.get("origin") == "scan" and (not doc or not page):
            raise forms.ValidationError("Scanned values need a source document and page.")
        if doc and page and not 1 <= page <= doc.page_count:
            raise forms.ValidationError("Source page is outside this document.")
        if data.get("origin") == "digital" and (doc or page):
            raise forms.ValidationError("Digital entries should not reference a scan.")
        if data.get("status") == "verified" and not data.get("value", "").strip():
            raise forms.ValidationError(
                "A blank value cannot be verified. Use Not applicable when appropriate."
            )
        return data


class JobRulesForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = Job
        fields = ["rules", "version"]
        widgets = {"rules": forms.CheckboxSelectMultiple}


class ReviewRowForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = Field
        fields = ["value", "unit", "status", "version"]
        widgets = {"value": forms.Textarea(attrs={"rows": 1})}

    def clean(self):
        data = super().clean()
        if data.get("status") == "verified" and not data.get("value", "").strip():
            self.add_error("status", "A blank reading cannot be verified.")
        return data


class ScopeForm(forms.ModelForm):
    report_scope = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple, label="Applicable report sections"
    )
    report_test_ids = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        label="Individual tests requested for the fixed certificate",
        required=False,
    )
    version = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = Job
        fields = [
            "customer",
            "sample_code",
            "test_series",
            "report_scope",
            "report_test_ids",
            "scope_note",
            "version",
        ]
        widgets = {"scope_note": forms.Textarea(attrs={"rows": 3})}
        labels = {"scope_note": "Test scope and reasons for excluded sections"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .assembly import TITLES
        from .fixed_template import TEMPLATE_NAME, TESTS

        self.fields["report_scope"].choices = list(TITLES.items())
        self.fields["report_test_ids"].choices = [(row[0], row[1]) for row in TESTS]
        self.fields["report_test_ids"].required = bool(
            self.instance.report_template and self.instance.report_template.name == TEMPLATE_NAME
        )
        self.fields["sample_code"].required = True
        self.fields["test_series"].required = True
        self.fields["scope_note"].required = True


class ReadingImportForm(forms.Form):
    file = forms.FileField(label="CSV export of laboratory readings")
    source_reference = forms.CharField(
        max_length=200,
        label="Source reference",
        help_text="Name of the spreadsheet, database export or measurement system.",
    )
