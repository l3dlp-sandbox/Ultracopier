#ifndef DISKSPACE_H
#define DISKSPACE_H

#include <QDialog>
#include <vector>
#include "../../../interface/PluginInterface_CopyEngine.h"
#include "StructEnumDefinition_CopyEngine.h"

namespace Ui {
class DiskSpace;
}

class DiskSpace : public QDialog
{
    Q_OBJECT

public:
    explicit DiskSpace(FacilityInterface * facilityEngine,std::vector<Diskspace> list,QWidget *parent = 0);
    ~DiskSpace();
    /// the clean seam for testing the not-enough-space dialog -- no test logic lives in the engine.
    virtual bool getAction() const;
    /// \brief create a DiskSpace dialog. nullptr factory in production -> the real GUI dialog.
    static DiskSpace *createInstance(FacilityInterface *facilityEngine,std::vector<Diskspace> list,QWidget *parent);
    static DiskSpace *(*overrideFactory)(FacilityInterface *,std::vector<Diskspace>,QWidget *);
private slots:
    void on_ok_clicked();
    void on_cancel_clicked();
private:
    Ui::DiskSpace *ui;
    bool ok;
};

#endif // DISKSPACE_H
