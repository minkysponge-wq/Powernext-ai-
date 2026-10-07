"""Validated filters shared by job history and report history."""

from django import forms


class HistoryFilter(forms.Form):
    q = forms.CharField(required=False, label="Search")
    customer = forms.CharField(required=False)
    sample = forms.CharField(required=False, label="Sample code")
    date_from = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}), label="From"
    )
    date_to = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}), label="To"
    )
    status = forms.ChoiceField(required=False)

    def __init__(self, *args, reports=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [("", "All statuses")] + (
            [("draft", "Draft"), ("approved", "Approved for export")]
            if reports
            else [
                ("empty", "Awaiting data"),
                ("review", "Needs review"),
                ("checked", "Readings reviewed"),
                ("approved", "Latest report approved"),
            ]
        )

    def clean(self):
        values = super().clean()
        if (
            values.get("date_from")
            and values.get("date_to")
            and values["date_from"] > values["date_to"]
        ):
            raise forms.ValidationError("The start date must be on or before the end date.")
        return values


def pagination_query(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()
