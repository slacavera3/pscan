#include "camoutputtrigger.h"
#include "ui_camoutputtrigger.h"

#include <QPainter>

CamOutputTrigger::CamOutputTrigger(QWidget *parent) :
    QWidget(parent),
    ui(new Ui::CamOutputTrigger)
{
    ui->setupUi(this);
    this->setLayout(ui->gridLayout_2);
    this->setMaximumHeight(280);

    m_tgrOutAttr.nTgrOutPort= 0;
    m_tgrOutAttr.TgrPort1.nTgrOutMode = TUOPT_READEND;
    m_tgrOutAttr.TgrPort1.nEdgeMode = TUOPT_RISING;
    m_tgrOutAttr.TgrPort1.nDelayTm = 0;
    m_tgrOutAttr.TgrPort1.nWidth = 5000;

    m_tgrOutAttr.TgrPort2.nTgrOutMode = TUOPT_EXPGLOBAL;
    m_tgrOutAttr.TgrPort2.nEdgeMode = TUOPT_RISING;
    m_tgrOutAttr.TgrPort2.nDelayTm = 0;
    m_tgrOutAttr.TgrPort2.nWidth = 5000;

    m_tgrOutAttr.TgrPort3.nTgrOutMode = TUOPT_EXPSTART;
    m_tgrOutAttr.TgrPort3.nEdgeMode = TUOPT_RISING;
    m_tgrOutAttr.TgrPort3.nDelayTm = 0;
    m_tgrOutAttr.TgrPort3.nWidth = 5000;

    m_tgrOut.nTgrOutPort = 0;
    m_tgrOut.nTgrOutMode = TUOPT_GND;
    m_tgrOut.nEdgeMode = TUOPT_RISING;
    m_tgrOut.nDelayTm = 0;
    m_tgrOut.nWidth = 5000;

    initRange();
    updateValue();
}

CamOutputTrigger::~CamOutputTrigger()
{
    delete ui;
}

void CamOutputTrigger::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    QRect drawRect(0, 0, this->geometry().width(), this->geometry().height());
    drawRect.setTop(drawRect.top() + 24);
    drawRect.setRight(drawRect.right() - 1);
    drawRect.setBottom(drawRect.bottom() - 2);
    painter.drawRect(drawRect);
}

void CamOutputTrigger::initRange()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    QStringList portList;
    portList << "1" << "2" << "3";
    ui->comboBoxPort->blockSignals(true);
    ui->comboBoxPort->clear();
    ui->comboBoxPort->addItems(portList);
    ui->comboBoxPort->blockSignals(false);

    QStringList kindList;
    kindList << "Exposure Start" << "Readout End" << "Global Exposure" << "Low" << "High";
    ui->comboBoxKind->blockSignals(true);
    ui->comboBoxKind->clear();
    ui->comboBoxKind->addItems(kindList);
    ui->comboBoxKind->blockSignals(false);

}

void CamOutputTrigger::updateValue()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int paramValue = 0;

    // Get the current triggerout enable value
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_ENABLETRIOUT, &paramValue))
    {
        ui->checkBoxEnable->setChecked(paramValue);
    }
    else
    {
        ui->checkBoxEnable->hide();
    }

    if (TUCAMRET_SUCCESS == TUCAM_Cap_GetTriggerOut(hIdxTUCam, &m_tgrOut))
    {
        m_tgrOutAttr.nTgrOutPort = m_tgrOut.nTgrOutPort;
        switch(m_tgrOut.nTgrOutPort)
        {
        case TUPORT_ONE:
            m_tgrOutAttr.TgrPort1.nTgrOutMode = m_tgrOut.nTgrOutMode;
            m_tgrOutAttr.TgrPort1.nEdgeMode = m_tgrOut.nEdgeMode;
            m_tgrOutAttr.TgrPort1.nDelayTm = m_tgrOut.nDelayTm;
            m_tgrOutAttr.TgrPort1.nWidth = m_tgrOut.nWidth;
            break;

        case TUPORT_TWO:
            m_tgrOutAttr.TgrPort2.nTgrOutMode = m_tgrOut.nTgrOutMode;
            m_tgrOutAttr.TgrPort2.nEdgeMode = m_tgrOut.nEdgeMode;
            m_tgrOutAttr.TgrPort2.nDelayTm = m_tgrOut.nDelayTm;
            m_tgrOutAttr.TgrPort2.nWidth = m_tgrOut.nWidth;
            break;

        case TUPORT_THREE:
            m_tgrOutAttr.TgrPort3.nTgrOutMode = m_tgrOut.nTgrOutMode;
            m_tgrOutAttr.TgrPort3.nEdgeMode = m_tgrOut.nEdgeMode;
            m_tgrOutAttr.TgrPort3.nDelayTm = m_tgrOut.nDelayTm;
            m_tgrOutAttr.TgrPort3.nWidth = m_tgrOut.nWidth;
            break;

        default:
            break;
        }
        ui->comboBoxPort->setCurrentIndex(m_tgrOut.nTgrOutPort);

        UpdateDelay(m_tgrOut.nDelayTm);

        UpdateWidth(m_tgrOut.nWidth);

        UpdateRadio();

        EnableOutPutTgrControl(m_tgrOut.nTgrOutMode);
    }
    else
    {
        ui->checkBoxEnable->setEnabled(false);

        ui->comboBoxPort->setEnabled(false);
        ui->comboBoxKind->setEnabled(false);

        ui->rbRising->setEnabled(false);
        ui->rbFalling->setEnabled(false);

        ui->pbOk->setEnabled(false);
        ui->pbWOk->setEnabled(false);
    }
}

void CamOutputTrigger::UpdateDelay(int nDelay)
{
    int nSec = (nDelay / (1000 * 1000)) % 60;
    int nMs  = (nDelay / 1000) % 1000;
    int nUs  = nDelay % 1000;

    ui->spbSec->setValue(nSec);
    ui->spbMs->setValue(nMs);
    ui->spbUs->setValue(nUs);
}

void CamOutputTrigger::UpdateWidth(int nWidth)
{
    int nSec = (nWidth / (1000 * 1000)) % 60;
    int nMs  = (nWidth / 1000) % 1000;
    int nUs  = nWidth % 1000;

    ui->spinBoxWSec->setValue(nSec);
    ui->spinBoxWMs->setValue(nMs);
    ui->spinBoxWUs->setValue(nUs);
}

void CamOutputTrigger::UpdateRadio()
{
    if(m_tgrOut.nEdgeMode == TUOPT_RISING)
    {
       ui->rbRising->setChecked(true);
       ui->rbFalling->setChecked(false);
    }
    else
    {
        ui->rbRising->setChecked(false);
        ui->rbFalling->setChecked(true);
    }

    ui->comboBoxKind->blockSignals(true);
    switch (m_tgrOut.nTgrOutMode)
    {
    case TUOPT_VCC:
    {
        ui->comboBoxKind->setCurrentIndex(4);
    }
        break;
    case TUOPT_EXPSTART:
    {
        ui->comboBoxKind->setCurrentIndex(0);
    }
        break;
    case TUOPT_EXPGLOBAL:
    {
        ui->comboBoxKind->setCurrentIndex(2);
    }
        break;
    case TUOPT_READEND:
    {
        ui->comboBoxKind->setCurrentIndex(1);

    }
        break;
    case TUOPT_GND:
    default:
    {
        ui->comboBoxKind->setCurrentIndex(3);
    }
        break;
    }
    ui->comboBoxKind->blockSignals(false);
}

void CamOutputTrigger::UpdateOutPutTgr()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    m_tgrOut.nDelayTm = ui->spbSec->value() *1000 * 1000 + ui->spbMs->value() * 1000 + ui->spbUs->value();
    if (m_tgrOut.nDelayTm > 10000000)
    {
        m_tgrOut.nDelayTm = 10000000;
        UpdateDelay(m_tgrOut.nDelayTm);
    }

    m_tgrOut.nWidth = ui->spinBoxWSec->value() *1000 * 1000 + ui->spinBoxWMs->value() * 1000 + ui->spinBoxWUs->value();
    if ( m_tgrOut.nWidth > 10000000)
    {
        m_tgrOut.nWidth = 10000000;
        UpdateWidth( m_tgrOut.nWidth);
    }

    if ( m_tgrOut.nWidth < 1)
    {
        m_tgrOut.nWidth = 1;
        UpdateWidth( m_tgrOut.nWidth);
    }

    switch(m_tgrOut.nTgrOutPort)
    {
    case TUPORT_ONE:
        m_tgrOutAttr.TgrPort1.nTgrOutMode = m_tgrOut.nTgrOutMode;
        m_tgrOutAttr.TgrPort1.nEdgeMode   = m_tgrOut.nEdgeMode;
        m_tgrOutAttr.TgrPort1.nDelayTm    = m_tgrOut.nDelayTm;
        m_tgrOutAttr.TgrPort1.nWidth      = m_tgrOut.nWidth;
        break;

    case TUPORT_TWO:
        m_tgrOutAttr.TgrPort2.nTgrOutMode = m_tgrOut.nTgrOutMode;
        m_tgrOutAttr.TgrPort2.nEdgeMode   = m_tgrOut.nEdgeMode;
        m_tgrOutAttr.TgrPort2.nDelayTm    = m_tgrOut.nDelayTm;
        m_tgrOutAttr.TgrPort2.nWidth      = m_tgrOut.nWidth;
        break;

    case TUPORT_THREE:
        m_tgrOutAttr.TgrPort3.nTgrOutMode = m_tgrOut.nTgrOutMode;
        m_tgrOutAttr.TgrPort3.nEdgeMode   = m_tgrOut.nEdgeMode;
        m_tgrOutAttr.TgrPort3.nDelayTm    = m_tgrOut.nDelayTm;
        m_tgrOutAttr.TgrPort3.nWidth      = m_tgrOut.nWidth;
        break;
    default:
        break;
    }

    TUCAM_Cap_SetTriggerOut(hIdxTUCam, m_tgrOut);
    EnableOutPutTgrControl(m_tgrOut.nTgrOutMode);
}

void CamOutputTrigger::EnableOutPutTgrControl(int nKind)
{
    switch (nKind)
    {
    case TUOPT_GND:
    case TUOPT_VCC:
    {
        ui->rbRising->setEnabled(false);
        ui->rbFalling->setEnabled(false);

        ui->spbSec->setEnabled(false);
        ui->spbMs->setEnabled(false);
        ui->spbUs->setEnabled(false);

        ui->spinBoxWSec->setEnabled(false);
        ui->spinBoxWMs->setEnabled(false);
        ui->spinBoxWUs->setEnabled(false);
    }
        break;
    case TUOPT_EXPGLOBAL:
    {
        ui->rbRising->setEnabled(true);
        ui->rbFalling->setEnabled(true);

        ui->spbSec->setEnabled(true);
        ui->spbMs->setEnabled(true);
        ui->spbUs->setEnabled(true);

        ui->spinBoxWSec->setEnabled(false);
        ui->spinBoxWMs->setEnabled(false);
        ui->spinBoxWUs->setEnabled(false);
    }
        break;
    default:
    {
        ui->rbRising->setEnabled(true);
        ui->rbFalling->setEnabled(true);

        ui->spbSec->setEnabled(true);
        ui->spbMs->setEnabled(true);
        ui->spbUs->setEnabled(true);

        ui->spinBoxWSec->setEnabled(true);
        ui->spinBoxWMs->setEnabled(true);
        ui->spinBoxWUs->setEnabled(true);
    }
        break;
    }
}

void CamOutputTrigger::on_checkBoxEnable_clicked(bool checked)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ENABLETRIOUT, checked);
}

void CamOutputTrigger::on_comboBoxPort_currentIndexChanged(int index)
{
    m_tgrOut.nTgrOutPort = index;

    switch(index)
    {
    case TUPORT_ONE:
        m_tgrOut.nTgrOutMode  = m_tgrOutAttr.TgrPort1.nTgrOutMode;
        m_tgrOut.nEdgeMode    = m_tgrOutAttr.TgrPort1.nEdgeMode;
        m_tgrOut.nDelayTm     = m_tgrOutAttr.TgrPort1.nDelayTm;
        m_tgrOut.nWidth       = m_tgrOutAttr.TgrPort1.nWidth;
        break;

    case TUPORT_TWO:
        m_tgrOut.nTgrOutMode  = m_tgrOutAttr.TgrPort2.nTgrOutMode;
        m_tgrOut.nEdgeMode    = m_tgrOutAttr.TgrPort2.nEdgeMode;
        m_tgrOut.nDelayTm     = m_tgrOutAttr.TgrPort2.nDelayTm;
        m_tgrOut.nWidth       = m_tgrOutAttr.TgrPort2.nWidth;
        break;

    case TUPORT_THREE:
        m_tgrOut.nTgrOutMode  = m_tgrOutAttr.TgrPort3.nTgrOutMode;
        m_tgrOut.nEdgeMode    = m_tgrOutAttr.TgrPort3.nEdgeMode;
        m_tgrOut.nDelayTm     = m_tgrOutAttr.TgrPort3.nDelayTm;
        m_tgrOut.nWidth       = m_tgrOutAttr.TgrPort3.nWidth;
        break;
    default:
        break;
    }

    UpdateDelay(m_tgrOut.nDelayTm );

    UpdateWidth(m_tgrOut.nWidth);

    UpdateRadio();

    UpdateOutPutTgr();
}

void CamOutputTrigger::on_comboBoxKind_currentIndexChanged(int index)
{
    ///kindList << "Exposure Start" << "Readout End" << "Global Exposure" << "Low" << "High";
    switch (index)
    {
    case 1:
        m_tgrOut.nTgrOutMode = TUOPT_READEND;
        break;
    case 2:
        m_tgrOut.nTgrOutMode = TUOPT_EXPGLOBAL;
        break;
    case 3:
        m_tgrOut.nTgrOutMode = TUOPT_GND;
        break;
    case 4:
        m_tgrOut.nTgrOutMode = TUOPT_VCC;
        break;
    default:
        m_tgrOut.nTgrOutMode = TUOPT_EXPSTART;
        break;
    }

    UpdateRadio();
    UpdateOutPutTgr();
}

void CamOutputTrigger::on_rbRising_clicked()
{
    m_tgrOut.nEdgeMode = TUOPT_RISING;
    UpdateRadio();
    UpdateOutPutTgr();
}

void CamOutputTrigger::on_rbFalling_clicked()
{
    m_tgrOut.nEdgeMode = TUOPT_FAILING;
    UpdateRadio();
    UpdateOutPutTgr();
}

void CamOutputTrigger::on_pbOk_clicked()
{
    UpdateOutPutTgr();
}

void CamOutputTrigger::on_pbWOk_clicked()
{
    UpdateOutPutTgr();
}
