#ifndef CAMROI_H
#define CAMROI_H

#include <QWidget>

namespace Ui {
class CamRoi;
}

class CamRoi : public QWidget
{
    Q_OBJECT

public:
    explicit CamRoi(QWidget *parent = 0);
    ~CamRoi();

    void initRoiParameter();
    void updateRoiState(bool isRoi);

protected:
    void paintEvent(QPaintEvent *event);

private slots:
    void on_pbSet_clicked();

private:
    Ui::CamRoi *ui;

    bool m_isRoi;
    int m_maxWidth;
    int m_maxHeight;
};

#endif // CAMROI_H
