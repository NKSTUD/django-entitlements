from django.db import models


class Org(models.Model):
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class Project(models.Model):
    org = models.ForeignKey(Org, on_delete=models.CASCADE, related_name="projects")

    def __str__(self):
        return f"project {self.pk}"
