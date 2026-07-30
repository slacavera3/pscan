#include "camimagesave.h"
#include "ui_camimagesave.h"

#include <QDir>
#include <QPainter>
#include <QFileDialog>
#include <QMessageBox>

#include "camobject.h"

CamImageSave::CamImageSave(QWidget *parent) :
    QWidget(parent),
    ui(new Ui::CamImageSave),
    m_totalFrames(1),
    m_countFormat(1),
    m_savedFormat(TUFMT_TIF),
    m_intervalTime(1000),
    m_savePath(""),
    m_imageName("TS")
{
    ui->setupUi(this);
    this->setLayout(ui->gridLayout);
    this->setMaximumHeight(140);

    initImageSaveValue();
    bindConnections();
}

CamImageSave::~CamImageSave()
{
    delete ui;
}

void CamImageSave::initImageSaveValue()
{
    m_imageName = "TS";
    m_totalFrames = 1;
    m_savedFormat = TUFMT_TIF;
    m_savePath = CamObject::getInstance()->getSavedPath();

    ui->lePath->setText(m_savePath);
    ui->leIntervalTime->setText(QString::number(m_intervalTime));
    ui->leTotalFrames->setText(QString::number(m_totalFrames));

    ui->cbTiff->setChecked(true);
    ui->cbPng->setChecked(false);
    ui->cbJpg->setChecked(false);
    ui->cbBmp->setChecked(false);
    ui->cbRaw->setChecked(false);
}

void CamImageSave::bindConnections()
{
    WaittingThread *wait = CamObject::getInstance()->getWaittingThread();

    connect(wait, SIGNAL(signalSaving(int)), this, SLOT(slotSaving(int)));
    connect(wait, SIGNAL(signalSavingFinished()), this, SLOT(slotSavingFinished()));
}

void CamImageSave::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    QRect drawRect(0, 0, this->geometry().width(), this->geometry().height());
    drawRect.setTop(drawRect.top() + 24);
    drawRect.setRight(drawRect.right() - 1);
    drawRect.setBottom(drawRect.bottom() - 2);
    painter.drawRect(drawRect);
}

void CamImageSave::on_pbSave_clicked()
{
    if (CamObject::getInstance()->isSaving())
    {
        CamObject::getInstance()->stopSavingImages();
        ui->pbSave->setText(tr("Save"));
    }
    else
    {
        m_intervalTime = ui->leIntervalTime->text().toInt();
        m_totalFrames = ui->leTotalFrames->text().toInt();

        if (m_totalFrames < 1)
        {
            QMessageBox::information(NULL, "Tips", "Total frames must more than 1!", QMessageBox::Ok);
            return;
        }

        // Is directory exists
        QDir dir(m_savePath);
        if(!dir.exists())
        {
            dir.mkpath(m_savePath);
        }

        CamObject::getInstance()->startSavingImages(m_totalFrames, m_intervalTime, m_savedFormat);
        ui->pbSave->setText(tr("Stop Save"));
    }
}

void CamImageSave::on_pbBrowse_clicked()
{
    QString filePath = CamObject::getInstance()->getSavedPath();
    filePath = QFileDialog::getExistingDirectory(nullptr, "Select path", ".");
    if (!filePath.isEmpty())
    {
        ui->lePath->setText(filePath);
        CamObject::getInstance()->setSavedPath(ui->lePath->text());
    }
}

void CamImageSave::on_cbTiff_clicked(bool checked)
{
    if (checked)
    {
        m_savedFormat |= TUFMT_TIF;
        m_countFormat++;
    }
    else
    {
        if (m_countFormat > 1)
        {
            m_savedFormat &= ~TUFMT_TIF;
            m_countFormat--;
        }
        else
        {
            ui->cbTiff->setChecked(true);
        }
    }
}

void CamImageSave::on_cbJpg_clicked(bool checked)
{
    if (checked)
    {
        m_savedFormat |= TUFMT_JPG;
        m_countFormat++;
    }
    else
    {
        if (m_countFormat > 1)
        {
            m_savedFormat &= ~TUFMT_JPG;
            m_countFormat--;
        }
        else
        {
            ui->cbJpg->setChecked(true);
        }
    }
}

void CamImageSave::on_cbPng_clicked(bool checked)
{
    if (checked)
    {
        m_savedFormat |= TUFMT_PNG;
        m_countFormat++;
    }
    else
    {
        if (m_countFormat > 1)
        {
            m_savedFormat &= ~TUFMT_PNG;
            m_countFormat--;
        }
        else
        {
            ui->cbPng->setChecked(true);
        }
    }
}

void CamImageSave::on_cbBmp_clicked(bool checked)
{
    if (checked)
    {
        m_savedFormat |= TUFMT_BMP;
        m_countFormat++;
    }
    else
    {
        if (m_countFormat > 1)
        {
            m_savedFormat &= ~TUFMT_BMP;
            m_countFormat--;
        }
        else
        {
            ui->cbBmp->setChecked(true);
        }
    }
}

void CamImageSave::on_cbRaw_clicked(bool checked)
{
    if (checked)
    {
        m_savedFormat |= TUFMT_RAW;
        m_countFormat++;
    }
    else
    {
        if (m_countFormat > 1)
        {
            m_savedFormat &= ~TUFMT_RAW;
            m_countFormat--;
        }
        else
        {
            ui->cbRaw->setChecked(true);
        }
    }
}

void CamImageSave::slotSaving(int index)
{
    char saveMsg[64] = {0};
    sprintf(saveMsg, "Saved %d/%d success!", (index * m_countFormat), (m_countFormat * m_totalFrames));
    ui->labvSavedMsg->setText(saveMsg);
}

void CamImageSave::slotSavingFinished()
{
    ui->labvSavedMsg->setText("");
    ui->pbSave->setText(tr("Save"));
}
