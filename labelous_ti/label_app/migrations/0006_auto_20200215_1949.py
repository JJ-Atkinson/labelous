# Generated by Django 3.0.3 on 2020-02-16 01:49

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('label_app', '0005_auto_20200215_1245'),
    ]

    operations = [
        migrations.AlterField(
            model_name='annotation',
            name='last_edit_time',
            field=models.DateTimeField(),
        ),
        migrations.AlterField(
            model_name='polygon',
            name='last_edit_time',
            field=models.DateTimeField(),
        ),
    ]